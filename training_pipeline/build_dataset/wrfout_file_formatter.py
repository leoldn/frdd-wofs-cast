
import sys, os 
sys.path.insert(0, os.path.dirname(os.path.dirname(os.getcwd())))


import xarray as xr
import pandas as pd
import numpy as np 

from glob import glob
import os

from datetime import datetime, timedelta
import itertools

from wofscast.utils import run_parallel, to_iterator 


VARS_3D_TO_KEEP = ['U', 'V', 'W', 'T', 'PH', 'PHB', 'QVAPOR']
VARS_2D_TO_KEEP = ['T2', 'RAINNC', 'COMPOSITE_REFL_10CM', 'UP_HELI_MAX']
CONSTANTS = ['HGT', 'XLAND']

VARS_TO_KEEP = VARS_3D_TO_KEEP + VARS_2D_TO_KEEP + CONSTANTS


def filter_dates(dates, month_range = ['04', '05', '06']):
    """
    Filter a list of dates to include only those in May and June.

    Args:
    - dates: A list of dates in 'YYYYMMDD' format.

    Returns:
    - A list of dates that fall in May and June.
    """
    # Filter dates where the month is either May (05) or June (06)
    filtered_dates = [date for date in dates if date[4:6] in month_range]
    
    return filtered_dates



class FileFormatter: 
    """
        FileFormatter reformats the WoFS WRFOUT files for use with the GraphCast code.
        
        The formatter also reduces the WRFOUT file size by reducing the 
        number of vertical levels and reducing the domain size. 
        
        duration_minutes (int): Duration in minutes to load files for.
        timestep_minutes (int): Time step in minutes between each file.
        offset (int) : Offset after initialization (in minutes). Useful 
                       for grabbing WoFS files after model spin-up.
    """
    
    def __init__(self,  n_jobs = None, 
                 duration_minutes=60, 
                 timestep_minutes=10, 
                 offset=60, 
                 domain_size=150, 
                 out_path = '/work/mflora/wofs-cast-data/datasets', 
                 debug=False):
        
        self.duration_minutes = duration_minutes
        self.timestep_minutes = timestep_minutes
        self.offset = offset
        self.domain_size = domain_size
        self.out_path = out_path
        
        self.n_expected_files = (self.duration_minutes // self.timestep_minutes) + 1 
        
        self.time_resolution = f"{timestep_minutes}min"
        
        if n_jobs is None:
            self.n_jobs = mp.cpu_count() // 6
        else:
            self.n_jobs = n_jobs
            
        self.debug = debug
        
        self._var_dim_map = {'U': 'west_east_stag', 
                       'V': 'south_north_stag',
                       'W': 'bottom_top_stag',
                       'GEOPOT': 'bottom_top_stag'
                      }
        
        
    def run(self, file_paths, single_case=False):
        """
        Adapted to process files generated by gen_file_paths in parallel.
        """
        print('Loading lat, lon, and variables to drop...')
        drop_vars, lat_1d, lon_1d = self.return_lat_lon_and_drop_vars()
    
        if self.debug or single_case: 
            return self.process_wofs_data(file_paths[0], drop_vars, lat_1d, lon_1d)
    
        #process_wofs_data(data_paths, drop_vars, lat_1d, lon_1d)
        args_iterator = to_iterator(file_paths, [drop_vars], [lat_1d], [lon_1d])

        results = run_parallel(
            self.process_wofs_data,
            args_iterator,
            nprocs_to_use=self.n_jobs,
            description='WRF Data Processing',
            kwargs={}, 
        )
        
        return results

    def compute_full_geopot(self, ds):
        """Combine the base and perturbation geopotential height"""
         # Combine geopotential perturbation + base state
        ds['GEOPOT'] = ds['PH'] + ds['PHB']
        ds = ds.drop_vars(['PH', 'PHB'])
        
        return ds 
    
    def rename_coords(self, ds):
        """Renaming coordinate variables to align with the ERA5 naming convention"""
         # Renaming coordinate variables to align with the ERA5 naming convention.
        return ds.rename({'Time': 'time', 'bottom_top' :'level', 
                    #'XLAT': 'latitude', 'XLONG' : 'longitude', 
                    'south_north' : 'lat', 'west_east' : 'lon'
               })
    
    # Function to process each dataset
    def process_wofs_data(self, data_paths, drop_vars, lat_1d, lon_1d):
        """ Process a single set of WRFOUT files"""
        drop_vars += ['XLAT', 'XLONG', 'XTIME']
        
        # Perform initial error checking and abort
        # early if needed. 
        if len(data_paths) == 0:
            return "Did not process, no files!"
        
        if len(data_paths) != self.n_expected_files:
            print(data_paths[0], 'Not enough time files, passing...')
            return 'Not enough time files, passing...'
    
        fname = self.create_filename_from_list(data_paths)
        year = self.get_year_from_path(data_paths[0])
        out_path = os.path.join(self.out_path, year, fname)
    
        if os.path.exists(out_path) and not self.debug:
            return "File already processed!"
    
        # Load the data. 
        ds = self.load_and_concatenate_datasets(data_paths, drop_vars)   

        ds = self.reset_negative_water_vapor(ds)
        
        # Combine geopotential perturbation + base state
        ds = self.compute_full_geopot(ds)
        
        # Renaming coordinate variables to align with the ERA5 naming convention.
        ds = self.rename_coords(ds)

        # Destagger the wind and geopotential fields 
        ds = self.destagger(ds)

        # Subset the vertical levels (every N layers). 
        ds = ds.isel(level=ds.level[::3].values)
        
        # Add 300. to make it properly Kelvins, so we can convert to deg C/F. 
        ds['T']+=300. 
        
        # Assign the 2D versions of 'xlat' and 'xlon' back to the dataset as coordinates
        # Latitude and longitude are expected to be 1d vectors. 
        ds = ds.assign_coords(lat=lat_1d, lon=lon_1d)

        # Convert negative longitude values to 0-360 range and update the Dataset
        #ds['lon'] = xr.where(ds['lon'] < 0, ds['lon'] + 180, ds['lon'])

        ds = self.add_time_dim(ds, data_paths)

        # Add level coordinate to the dataset 
        ds = ds.assign_coords(level=ds.level)

        # Assuming 'lat' and 'lon' are the coordinate names for the grid dimensions
        n_lat, n_lon = ds.dims['lat'], ds.dims['lon']
    
        # Unaccumulate rainfall
        ds = self.unaccum_rainfall(ds)
    
        ##ds = self.resize(ds)
        if self.debug:
            print(f"Processed result for {out_path}")
            return ds 
        
        ## Define encoding with compression
        encoding = {var: {'zlib': True, 'complevel': 5} for var in ds.data_vars}
    
        ds.to_netcdf(out_path, encoding=encoding)
            
        return f"Processed result for {out_path}"
    
    def unaccum_rainfall(self, ds):
        """
        Calculate the difference in accumulated rainfall ('RAINNC') at each time step,
        with an assumption that the first time step starts with zero rainfall.
    
        Parameters:
        - ds: xarray.Dataset containing the 'RAINNC' variable
    
        Returns:
            - Modified xarray.Dataset with the new variable 'RAINNC_DIFF'
        """
        # Calculate the difference along the time dimension
        rain_diff = ds['RAINNC'].diff(dim='time')
    
        # Prepend a zero for the first time step. This assumes that the difference
        # for the first time step is zero since there's no previous time step to compare.
        # We use np.concatenate to add the zero at the beginning. Ensure that the dimensions match.
        # Adjust dimensions and coordinates according to your dataset's specific setup.
        initial_zero = xr.zeros_like(ds['RAINNC'].isel(time=0))
        rain_diff_with_initial = xr.concat([initial_zero, rain_diff], dim='time')
    
        # Add the computed difference back to the dataset as a new variable
        ds['RAIN_AMOUNT'] = rain_diff_with_initial
    
        ds = ds.drop_vars(['RAINNC'])
        
        return ds
        
    
    def reset_negative_water_vapor(self, ds):
        """Set negative QVAPOR to zero"""
        if 'QVAPOR' in ds.data_vars: 
            ds['QVAPOR'] = ds['QVAPOR'].where(ds['QVAPOR'] > 0, 0)

        return ds 
    
    def resize(self, ds):
        """Resize the domain"""
        n_lat, n_lon = ds.dims['lat'], ds.dims['lon']
        
        start_lat, start_lon = (n_lat - self.domain_size) // 2, (n_lon - self.domain_size) // 2
        end_lat, end_lon = start_lat + self.domain_size, start_lon + self.domain_size
        
        # Subsetting the dataset to the central size x size grid
        ds_subset = ds.isel(lat=slice(start_lat, end_lat), lon=slice(start_lon, end_lon))
        
        return ds_subset
    
    def add_time_dim(self, ds, data_paths):
        """Add time dimensions/coords to make use of GraphCast data utils"""
         # Formating the time dimension for the graphcast code. 
        # Define the start time for the first file in the data paths. 
        fname = os.path.basename(data_paths[0])
        start_time_dt = datetime.strptime(fname, 'wrfwof_d01_%Y-%m-%d_%H:%M:%S')

        start_time = pd.Timestamp(start_time_dt)

        num_time_points = ds.sizes['time']

        # Generate the datetime range
        time_range = pd.date_range(start=start_time, periods=num_time_points, freq=self.time_resolution)
        ds['time'] = time_range
        
        ds = ds.assign_coords(datetime=time_range)

        # Convert 'time' dimension to timedeltas from the first time point
        time_deltas = (ds['time'] - ds['time'][0]).astype('timedelta64[ns]')
        ds['time'] = time_deltas
        
        return ds 
    
    
    def destagger(self, ds):
        """
        General function to destagger any given variables along their specified dimensions.

        Parameters:
        ds : xarray.Dataset
            The dataset containing the staggered variables.
        var_dim_map : dict
            A mapping of variable names to their staggered dimensions.
            For example: {'U': 'west_east_stag', 'V': 'south_north_stag'}

        Returns:
        ds : xarray.Dataset
            The dataset with destaggered variables.
        """
        for var, stag_dim in self._var_dim_map.items():
            # Calculate the destaggered variable
            destaggered_var = 0.5 * (ds[var] + ds[var].roll({stag_dim: -1}, roll_coords=False))
            # Trim the last index of the staggered dimension
            destaggered_var = destaggered_var.isel({stag_dim: slice(None, -1)})
            # Rename the staggered dimension if a naming convention is provided
            # This step can be customized or made optional based on specific requirements
            new_dim_name = self.get_new_dim_name(stag_dim)  # Implement this method based on your context
            destaggered_var = destaggered_var.rename({stag_dim: new_dim_name})
            # Update the dataset with the destaggered variable
            ds[var] = destaggered_var

        return ds

    def get_new_dim_name(self, stag_dim):
        """ Rename the existing staggered coordinates to the destaggered name for consistency."""
        dim_name_map = {'west_east_stag': 'lon', 'south_north_stag': 'lat', 'bottom_top_stag': 'level'}
        return dim_name_map.get(stag_dim, stag_dim)
    
    
    def return_lat_lon_and_drop_vars(self):
        # Assuming a single latitude longitude grid for all WoFS cases!!
        this_path = glob(os.path.join('/work2/wof/realtime/FCST/2020/', 
                                      '20200507', '2300', 'ENS_MEM_01', 'wrfwof_d01_*'))[0]
        with xr.open_dataset(this_path) as this_ds:
            data_vars = this_ds.data_vars
            drop_vars = [v for v in data_vars if v not in VARS_TO_KEEP]
            this_ds = this_ds.compute()
    
            # Renaming coordinate variables to align with the ERA5 naming convention.
            this_ds = this_ds.rename({ 
                    'XLAT': 'latitude', 'XLONG' : 'longitude', 
                    'south_north' : 'lat', 'west_east' : 'lon'
               })
    
            # Latitude and longitude are expected to be 1d vectors. 
            lat_1d = this_ds['latitude'].isel(lon=0, Time=0)
            lon_1d = this_ds['longitude'].isel(lat=0, Time=0)

        return drop_vars, lat_1d, lon_1d
    
    def get_year_from_path(slef, file_path):
        """
        Extract the year from a given file path, assuming the year comes after the 'FCST' segment.

        Args:
        - file_path: The file path as a string.

        Returns:
        - The year as a string.
        """
        # Split the path into parts
        parts = file_path.split(os.sep)
    
        # Find the index of the 'FCST' segment
        try:
            fcst_index = parts.index('FCST')
            # The year should be the next segment
            year = parts[fcst_index + 1]
            return year
        except (ValueError, IndexError):
            # Handle the case where 'FCST' is not found or there is no segment after 'FCST'
            print("The file path does not contain 'FCST' followed by a year.")
            return None
    
    
    def parse_filename_datetime(self, filename):
        """
        Extract datetime object from a WRFOUT file path.
    
        Args:
            filename (str): Filename in the format wrfwof_d01_YYYY-MM-DD_HH:MM:SS
    
        Returns:
            datetime: Datetime object representing the timestamp in the filename.
        """
        # Convert string to datetime object
        return datetime.strptime(filename, 'wrfwof_d01_%Y-%m-%d_%H:%M:%S')

    def get_wrfwofs_files(self, directory_path, duration_minutes=60, timestep_minutes=10, offset=60):
        """
        Load files for a given duration and timestep.
    
        Args:
        directory_path (str): Path to the directory containing the files. 

        Returns:
            list: List of filenames that match the given duration and timestep.
        """

        # List all files in the directory
        files = glob(os.path.join(directory_path, 'wrfwof_d01_*'))
    
        try:
            files[0]
        except:
            print(os.path.join(directory_path, 'wrfwof_d01_*'))
            return [] 
    
        files.sort() 
    
        first_datetime = self.parse_filename_datetime(
            os.path.basename(files[0])) + timedelta(minutes=self.offset)
        end_datetime = first_datetime + timedelta(minutes=self.duration_minutes)
        current_datetime = first_datetime
    
        selected_files = []

        while current_datetime <= end_datetime:
            # Format the current datetime to match filename pattern
            datetime_pattern = current_datetime.strftime('%Y-%m-%d_%H:%M:%S')
            # Search for the file that matches the current datetime
            for file in files:
                if datetime_pattern in file:
                    selected_files.append(file)
                    break
            # Increment current_datetime by the timestep
            current_datetime += timedelta(minutes=self.timestep_minutes)
    
        return selected_files

    def create_filename_from_list(self, file_paths):
        """
        Create a filename based on the first and last elements of a list of file paths.

        Args:
            file_paths (list): A list of file paths.

        Returns:
            str: A string representing the generated filename, which includes the start and end datetime.
        """
        if not file_paths:
            return "No files provided"

        # Extract start time from the first element
        start_time = os.path.basename(file_paths[0]).replace('wrfwof_d01_', '')  
        # Extract end time from the last element
        end_time = os.path.basename(file_paths[-1]).replace('wrfwof_d01_', '')  
    
        # Format the filename
        ens_mem = os.path.basename(os.path.dirname(file_paths[-1])).split('_')[-1]
    
        filename = f"wrfwof_{start_time}_to_{end_time}__{self.time_resolution}__ens_mem_{int(ens_mem):02d}.nc"

        # Cleaning up the datetime format to remove colons and make it filesystem-friendly
        for char in [":"]:
            filename = filename.replace(char, "")
    
        return filename

    def load_and_concatenate_datasets(self, data_paths, drop_variables):

        datasets = [xr.load_dataset(path, drop_variables=drop_variables) for path in data_paths]
        combined_data = xr.concat(datasets, dim='Time')
        return combined_data

    def get_dir(self, base_path, date, init_time, mem):
        assert 1 <= mem <= 18, f'{mem} is not valid!'
        
        dir_path = os.path.join(base_path, date, init_time, f'ENS_MEM_{mem:02d}')
        
        if os.path.exists(dir_path):
            return dir_path 
        else:
            return os.path.join(base_path, date, init_time, f'ENS_MEM_{mem}')

    def gen_file_paths(self, base_path, dates, init_times, mems):
        for date, init_time, mem in itertools.product(dates, init_times, mems): 
            directory_path = self.get_dir(base_path, date, init_time, mem)
            yield self.get_wrfwofs_files(directory_path)
    