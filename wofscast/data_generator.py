#########################################
# Data Generator using Dask 
#########################################

from . import data_utils
from . import graphcast_lam as graphcast

import os
import xarray as xr
from glob import glob
import numpy as np
import pandas as pd
import dataclasses
import random 

from tqdm import tqdm
from dask.diagnostics import ProgressBar
import dask
from dask import delayed, compute
from dask.distributed import Client
import gc 

#from numba import jit
import numpy as np
from datetime import datetime, timedelta
import math

import jax.numpy as jnp 
from jax import jit
import jax


import fsspec


def to_static_vars(dataset):
    # Select the first time index for 'HGT' and 'XLAND' variables
    hgt_selected = dataset['HGT'].isel(time=0).drop('time')
    xland_selected = dataset['XLAND'].isel(time=0).drop('time')

    # Now, replace the 'HGT' and 'XLAND' in the original dataset with these selected versions
    dataset = dataset.drop_vars(['HGT', 'XLAND'])
    dataset['HGT'] = hgt_selected
    dataset['XLAND'] = xland_selected

    return dataset


class TOARadiationFlux:
    def __init__(self):
        # Solar constant in W/m^2
        self.S0 = 1361

    @staticmethod
    @jit
    def calculate_solar_declination(day_of_year):
        """Calculate solar declination as a function of day of the year."""
        return 23.45 * jnp.sin(jnp.radians((360 / 365) * (day_of_year - 81)))

    @staticmethod
    @jit#(nopython=True)
    def calculate_hour_angle(utc_hour, longitude):
        """Calculate solar hour angle based on UTC time."""
        # Convert longitude to equivalent time (1 hour per 15 degrees)
        longitude_time = longitude / 15.0
        
        # Calculate solar time from UTC time and longitude
        solar_time = utc_hour + longitude_time
        
        # Hour angle, considering each hour is 15 degrees of rotation
        hour_angle = (solar_time - 12) * 15
        return hour_angle

    @staticmethod
    @jit
    def calculate_solar_zenith_angle(latitude, declination, hour_angle):
        """Calculate solar zenith angle."""
        latitude_rad = jnp.radians(latitude)
        declination_rad = jnp.radians(declination)
        hour_angle_rad = jnp.radians(hour_angle)

        cos_zenith = jnp.sin(latitude_rad) * jnp.sin(declination_rad) + \
                     jnp.cos(latitude_rad) * jnp.cos(declination_rad) * jnp.cos(hour_angle_rad)
        zenith_angle = jnp.degrees(jnp.arccos(cos_zenith))
        return zenith_angle
    
        
    def calculate_flux(self, date_times, lat_grid, lon_grid):
        NT = len(date_times)
        NY, NX = lat_grid.shape
        flux = jnp.zeros((NT, NY, NX), dtype=jnp.float32)

        # Loop through each datetime, calculate declination once per datetime
        for i, datetime_obj in enumerate(date_times):
            day_of_year = datetime_obj.timetuple().tm_yday
            declination = self.calculate_solar_declination(day_of_year)

            # Vectorized computation over latitude and longitude grids
            for j in range(NY):
                hour_angle = self.calculate_hour_angle(datetime_obj.hour + datetime_obj.minute / 60, 
                                                       lon_grid[j])
                zenith_angle = self.calculate_solar_zenith_angle(lat_grid[j], declination, hour_angle)
                
                # Calculate radiation flux
                flux = flux.at[i, j, :].set(
                    jnp.where(zenith_angle < 90, self.S0 * jnp.cos(jnp.radians(zenith_angle)), 0))
                
        return flux


def add_local_solar_time(data: xr.Dataset) -> xr.Dataset:
    """    
    Adds sine and cosine-transformed local solar time variables to the dataset,
    adjusted for longitude, and replicated across latitude. Also adds 
    TOA (top-of-the-atmosphere) radiation. These variables are used as forcing
    inputs (known in the future; beyond the initial conditions) for the AI-NWP. 

    Args:
        data: The input dataset with 'time', 'lat', and 'lon' dimensions.

    Returns:
        xr.Dataset: The dataset with 'local_solar_time_sin' and 'local_solar_time_cos' variables added.
    """
    # Create an instance of TOARadiationFlux
    toa_radiation = TOARadiationFlux()
    
    time_dim = 'time'
    if {'Time'}.issubset(data.dims):
        time_dim = 'Time'
    
    if not {time_dim, 'lat', 'lon'}.issubset(data.dims):
        missing_dims = {time_dim, 'lat', 'lon'} - set(data.dims)
        raise ValueError(f"Missing dimensions in the dataset: {missing_dims}")

    # Calculate the local solar time adjustment
    local_hours = (data.coords['datetime'].dt.hour + data.coords['lon'] / 15.0) % 24

    # Convert local_hours to radians for sine and cosine
    radians = (local_hours * 2 * np.pi) / 24

    # Calculate sine and cosine for the local solar time
    local_solar_time_sin = np.sin(radians)
    local_solar_time_cos = np.cos(radians)

    # Create DataArrays with 'time' and 'lon' dimensions
    local_solar_time_sin_da = xr.DataArray(local_solar_time_sin, dims=(time_dim, 'lon'),
                                           coords={time_dim: data.coords[time_dim], 'lon': data.coords['lon']})
    local_solar_time_cos_da = xr.DataArray(local_solar_time_cos, dims=(time_dim, 'lon'),
                                           coords={time_dim: data.coords[time_dim], 'lon': data.coords['lon']})

    # Replicate values across 'lat' dimension by broadcasting with an array of ones shaped (lat,)
    ones_lat = xr.DataArray(np.ones(data.dims['lat']), dims=['lat'], coords={'lat': data.coords['lat']})
    local_solar_time_sin_da, _ = xr.broadcast(local_solar_time_sin_da, ones_lat)
    local_solar_time_cos_da, _ = xr.broadcast(local_solar_time_cos_da, ones_lat)

    # Assign to the dataset
    data['local_solar_time_sin'] = local_solar_time_sin_da.astype('float32')
    data['local_solar_time_cos'] = local_solar_time_cos_da.astype('float32')
    
    
    # Add TOA (top-of-the-atmo) radiation 
    # Calculate the TOA radiation flux for the defined dates, times, and grid
    '''
    lat_grid, lon_grid = np.meshgrid(data.lat, data.lon)  # Create 2D grid
    flux = toa_radiation.calculate_flux(pd.to_datetime(data.datetime.values), 
                                        lat_grid, lon_grid)
    
    data['toa_radiation'] = xr.DataArray(flux, dims=(time_dim, 'lat', 'lon'),
                                           coords={time_dim: data.coords[time_dim], 
                                                   'lat': data.coords['lat'],
                                                   'lon': data.coords['lon']})
    '''
    return data

def load_wofscast_data(paths, lead_times, task_config, client): 
    """Loads a large number of netcdf files into memory using dask.distributed.
    Useful storing the full dataset in CPU RAM and then offloading small subsets
    to the GPU RAM batch by batch. 
    
    paths: list of paths: Path to my custom wrfwof files 
    lead_times: slice of shortest to longest lead time in the wrfwof files
    task_config: graphcast.TaskConfig: An object containing useful variables for the input/target building
    client: dask.distributed.Client
    
    """
    # Load all the data into memory. 
    dataset = xr.open_mfdataset(paths, 
                                concat_dim='batch', 
                                parallel=True, 
                                combine='nested',
                                preprocess=add_local_solar_time
                                    ) 
    
    inputs, targets, forcings = data_utils.extract_inputs_targets_forcings(dataset,
                                                        target_lead_times=lead_times,
                                                        **dataclasses.asdict(task_config))
        
    # Convert the constant fields to time-independent (drop time dim) and transpose as needed
    inputs = to_static_vars(inputs)
        
    # Perform computation efficiently with dask.
    with ProgressBar():
        inputs, targets, forcings = dask.compute(inputs, targets, forcings)
        
    inputs = inputs.transpose('batch', 'time', 'lat', 'lon', 'level')
    targets = targets.transpose('batch', 'time', 'lat', 'lon', 'level')
    forcings = forcings.transpose('batch', 'time', 'lat', 'lon')
            
    return inputs, targets, forcings 


def wofscast_batch_generator(inputs, targets, forcings, batch_size=32, n_timesteps=1, seed=123):
    """Batcher for an xarray dataset. Useful for storing the full dataset in CPU RAM and then offloading small subsets
    to the GPU RAM batch by batch. Assumes 'inputs' and 'targets' are xarray DataArrays or Datasets."""
    ###np.random.seed(seed)  # Set the seed for reproducibility
    
    total_samples = len(inputs.batch)
    total_batches = total_samples // batch_size + (1 if total_samples % batch_size > 0 else 0)
    
    indices = np.random.permutation(total_samples)  # Shuffle indices
    
    targets = targets.isel(time=slice(0, n_timesteps))  # Pre-select timesteps
    forcings = forcings.isel(time=slice(0, n_timesteps))
    
    for batch_num in range(total_batches):
        batch_indices = indices[batch_num * batch_size : min((batch_num + 1) * batch_size, total_samples)]
        
        batch_inputs = inputs.isel(batch=batch_indices)
        batch_targets = targets.isel(batch=batch_indices)
        batch_forcings = forcings.isel(batch=batch_indices)
        
        yield batch_inputs, batch_targets, batch_forcings
        
    
def check_for_nans(dataset):

    # Iterate through each variable in the Dataset
    for var_name, data_array in dataset.items():
        # Find boolean mask of NaNs
        nan_mask = data_array.isnull()
    
        # Use np.where to find the indices of NaNs
        nan_indices = np.where(nan_mask)
    
        # `nan_indices` is a tuple of arrays, each array corresponds to indices along one dimension
        # Print the locations of NaNs
        print(f"NaN locations in {var_name}:")
        for dim, inds in zip(nan_mask.dims, nan_indices):
            print(f"  {dim}: {inds}") 
    
def read_netcdfs_dask(paths, dim, transform_func=None):
    """Reading multiple netcdf files into memory, using dask for efficiency"""
    @delayed
    def process_one_path(path):
        # use a context manager, to ensure the file gets closed after use
        with xr.open_dataset(path) as ds:
            # transform_func should do some sort of selection or
            # aggregation
            if transform_func is not None:
                ds = transform_func(ds)
            # load all data from the transformed dataset, to ensure we can
            # use it after closing each original file
            ds.load()
            return ds
        
    #datasets = [process_one_path(p) for p in tqdm(paths, desc="Loading WRFOUT files")]
    
    delayed_datasets = [process_one_path(p) for p in paths]
    
    with ProgressBar():
        datasets = compute(*delayed_datasets)
    
    combined = xr.concat(datasets, dim)
    
    return combined
    
def read_netcdfs(paths, dim, transform_func=None):
    """Reading multiple netcdf files into memory, using dask for efficiency"""
    def process_one_path(path):
        # use a context manager, to ensure the file gets closed after use
        with xr.open_dataset(path) as ds:
            # transform_func should do some sort of selection or
            # aggregation
            if transform_func is not None:
                ds = transform_func(ds)
            # load all data from the transformed dataset, to ensure we can
            # use it after closing each original file
            ds.load()
            return ds
        
    datasets = [process_one_path(p) for p in tqdm(paths, desc="Loading WRFOUT files")]
    
    combined = xr.concat(datasets, dim)
    
    return combined
    
def read_mfnetcdfs_dask(paths, dim, transform_func=None, chunks ={"time": 4}, load=True):
    """Read multiple NetCDF files into memory, using Dask for parallel loading."""
    # Absolutely, crucial to set threads_per_worker=1!!!!
    # https://forum.access-hive.org.au/t/netcdf-not-a-valid-id-errors/389/19
    #To summarise in this thread, it looks like a work-around in netcdf4-python to deal 
    #with netcdf-c not being thread safe was removed in 1.6.1. 
    #The solution (for now) is to make sure your cluster only uses 1 thread per worker.

    dataset = xr.open_mfdataset(paths, concat_dim=dim, combine='nested',
                                parallel=True, preprocess=transform_func,
                                chunks=chunks, )  # Adjust the chunking strategy as needed
    if load:
        with ProgressBar():
            loaded_dataset= dataset.compute()
        return loaded_dataset

    return dataset    
    
    
def open_mfdataset_batch(path, batch_chunk_size, concat_dim='batch'):
    """Using kerchunking, individual zarr or netcdf files are represented by individual json files. 
    We can then trick xarray into believing the jsons are individual zarr files and treat them as
    one file. This function uses dask.delayed to lazily load the individual jsons in parallel and 
    then re-chunks based on the batch chunk size for efficiently batch loading. 
    """
    @delayed
    def load_dataset_from_json(json_path):
        """Load a dataset from a Kerchunk JSON descriptor."""
        # Using fsspec to create a mapper from the JSON reference
        mapper = fsspec.get_mapper('reference://', fo=json_path, remote_protocol='file')
        # Load the dataset using xarray with the Zarr engine
        ds = xr.open_dataset(mapper, engine='zarr', consolidated=False, chunks={})
        
        return ds

    def load_and_concatenate(json_dir, concat_dim='batch'):
        """Load multiple datasets from JSON files and concatenate them along a specified dimension."""
        datasets = []
    
        # List all JSON files in the directory
        json_files = [os.path.join(json_dir, f) for f in os.listdir(json_dir) if f.endswith('.json')]
    
         # Load each dataset using Dask delayed and collect them in a list
        datasets = [load_dataset_from_json(json_file) for json_file in json_files]
    
        # Use Dask to compute the list of datasets
        datasets = dask.compute(*datasets)
    
        # Concatenate all datasets along the specified dimension
        combined_dataset = xr.concat(datasets, dim=concat_dim)
    
        for ds in datasets:
            ds.close() 
    
        return combined_dataset


    dataset =  load_and_concatenate(path, 
                                     concat_dim='batch')
    
    dataset = dataset.chunk({'batch': batch_chunk_size})
    
    return dataset 
    
    
    
    
    
def wofscast_data_generator(path, 
                            train_lead_times, 
                            task_config,
                            batch_chunk_size=256, 
                            client=None,):
    
    with xr.open_dataset(path, 
                         engine='zarr', 
                         consolidated=True, 
                         chunks={'batch' : batch_chunk_size}
                        ) as ds:
    
    #with open_mfdataset_batch(path, batch_chunk_size) as ds:
   
            total_samples = len(ds.batch)
            total_batches = total_samples // batch_chunk_size + (1 if total_samples % batch_chunk_size > 0 else 0)
    
            for batch_num in tqdm(range(total_batches), desc='Loading Zarr Batch..'):
                start_idx = batch_num * batch_chunk_size
                end_idx = min((batch_num + 1) * batch_chunk_size, total_samples)
                batch_indices = slice(start_idx, end_idx)  # Use slice for more efficient indexing
        
                # Load this batch into memory. 
                this_batch = ds.isel(batch=batch_indices)
        
                inputs, targets, forcings = data_utils.extract_inputs_targets_forcings(
                    this_batch,
                    target_lead_times=train_lead_times,
                    **dataclasses.asdict(task_config)
                )
        
                inputs = to_static_vars(inputs)
        
                dims = ('batch', 'time', 'lat', 'lon', 'level')

                inputs = inputs.transpose(*dims, missing_dims='ignore')
                targets = targets.transpose(*dims, missing_dims='ignore')
                forcings = forcings.transpose(*dims, missing_dims='ignore')

                inputs, targets, forcings = dask.compute(inputs, targets, forcings)
            
                yield inputs, targets, forcings 

    
''' 
def wofscast_data_generator(file_paths, train_lead_times, task_config, chunk_size=2500):
    # Helper function to divide file_paths into chunks
    def chunked_file_paths(file_paths, chunk_size):
        for i in range(0, len(file_paths), chunk_size):
            yield file_paths[i:i + chunk_size]

    # Your data processing loop, modified to process chunks of file paths
    with Client(n_workers=8, threads_per_worker=1) as c: 
    
        for file_path_chunk in chunked_file_paths(file_paths, chunk_size):
            #inputs, target, forcings = load_wofscast_data(file_path_chunk, train_lead_times, task_config, client)
        
            #dataset_result = read_netcdfs(file_path_chunk, dim='batch', transform_func=add_local_solar_time)
        
            dataset_result  = read_mfnetcdfs_dask(file_path_chunk, dim, transform_func=add_local_solar_time)
        
            # Check for NaNs!!
            ###check_for_nans(dataset)
        
            inputs, targets, forcings = data_utils.extract_inputs_targets_forcings(
                dataset_result,
                target_lead_times=train_lead_times,
                **dataclasses.asdict(task_config)
            )
        
            dataset_result.close() 
            del dataset_result
            gc.collect() 
        
            inputs = to_static_vars(inputs)
        
            inputs = inputs.transpose('batch', 'time', 'lat', 'lon', 'level')
            targets = targets.transpose('batch', 'time', 'lat', 'lon', 'level')
            forcings = forcings.transpose('batch', 'time', 'lat', 'lon')
      
        
            yield inputs, targets, forcings 
'''



        
        
        