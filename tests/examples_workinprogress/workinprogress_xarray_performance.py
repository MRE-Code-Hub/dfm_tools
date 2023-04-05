# -*- coding: utf-8 -*-
"""
Created on Tue Apr  4 11:41:56 2023

@author: veenstra

module load netcdf/v4.9.0_v4.6.0_intel22.2.0
nccopy -k 'netCDF-4' 'DCSM-FM_0_5nm_0000_his.nc' 'DCSM-FM_0_5nm_0000_his_netcdf4.nc'
"""

import datetime as dt
import dfm_tools as dfmt
import xarray as xr
from dask.diagnostics import ProgressBar


file_nc = r'P:\11208054-004-dcsm-fm\models\3D_DCSM-FM\2013-2017\B04_EOT20_RHO1_H1_H2\DFM_OUTPUT_DCSM-FM_0_5nm\DCSM-FM_0_5nm_0000_his.nc' #does not seem to be dask? >> no chunks visible

#performance measurements can be influenced by partly caching in memory
chunks = {} #Open: 1581 sec (26 min)  #Plot: 12646 sec (210 min)
chunks = {'time':1,'stations':10} #Open: 864 sec (14 min) #Plot: ?? sec (?? min)

print('>> performance test opening: ',end='')
dtstart = dt.datetime.now()
ds = xr.open_dataset(file_nc,chunks=chunks)
print(f'{(dt.datetime.now()-dtstart).total_seconds():.2f} sec')

ds = dfmt.preprocess_hisnc(ds)

print('>> performance test plotting: ',end='')
dtstart = dt.datetime.now()
ds_toplot = ds.salinity.isel(laydim=39).sel(stations='HOEKVHLD')
with ProgressBar():
    ds_toplot.plot()
print(f'{(dt.datetime.now()-dtstart).total_seconds():.2f} sec')
