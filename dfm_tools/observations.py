import re
import pandas as pd
import numpy as np
from urllib.request import urlopen
import unicodedata
import geopandas as gpd
from shapely import Point
import os
import xarray as xr
from dfm_tools.download import copernicusmarine_credentials, cds_credentials
from dfm_tools.data import get_dir_testdata
from erddapy import ERDDAP
import requests
from zipfile import ZipFile
from io import BytesIO
import functools
import tempfile
import ddlpy
import glob
import matplotlib.pyplot as plt
import matplotlib.dates as md
import shutil
import copernicusmarine
import cdsapi
import logging
from dfm_tools import settings

__all__ = ["ssh_catalog_subset",
           "ssh_catalog_toxynfile",
           "ssh_catalog_tokmlfile",
           "ssh_retrieve_data",
           "ssh_netcdf_overview",
           ]

CM_LOGGER = logging.getLogger("copernicusmarine")
logger = logging.getLogger(__name__)


def _make_hydrotools_consistent(ds):
    """
    to make resulting netcdf file consistent with hydro_tools matlab post-processing
    """
    
    # assert presence of time variable/dim (case sensitive)
    assert "time" in ds.variables
    assert "time" in ds.dims
    
    # assert presence and units of waterlevel variable
    #TODO: add/check for standard_name attr?
    assert "waterlevel" in ds.data_vars
    assert hasattr(ds["waterlevel"], "units")
    assert ds["waterlevel"].attrs["units"] == "m"
    
    ds["station_name"] = xr.DataArray(ds.attrs["station_name"]).astype("S64")
    ds["station_id"] = xr.DataArray(ds.attrs["station_id"]).astype("S64")
    x_attrs = dict(standard_name='longitude', units='degrees_east')
    y_attrs = dict(standard_name='latitude', units='degrees_north')
    ds["station_x_coordinate"] = xr.DataArray(ds.attrs["longitude"]).assign_attrs(x_attrs)
    ds["station_y_coordinate"] = xr.DataArray(ds.attrs["latitude"]).assign_attrs(y_attrs)
    # set waterlevel coords
    coords = ["station_name", "station_id", "station_x_coordinate", "station_y_coordinate"]
    ds = ds.set_coords(coords)
    return ds


def _remove_accents(input_str):
    # replace characters that otherwise would be dropped
    input_str_replaced = input_str.replace('ø','o')
    
    # convert string to ascii-only
    nfkd_form = unicodedata.normalize('NFKD', input_str_replaced)
    only_ascii = nfkd_form.encode('ASCII', 'ignore').decode('ASCII')
    
    # warn if characters were dropped
    if len(input_str) != len(only_ascii):
        logger.warning(
            f"_remove_accents() dropped characters: '{input_str}' became '{only_ascii}'"
            )
    return only_ascii


def _check_ssc_groups_valid(groups):
    list_validgroups = ['psmsl','ioc','ptwc','gloss','uhslc']
    if not isinstance(groups, list):
        groups = [groups]
    for groupname in groups:
        if groupname not in list_validgroups:
            raise ValueError(f"groupname should be one of {list_validgroups}, '{groupname}' is not valid")


def ssc_sscid_from_otherid(group_id, groupname):
    """
    sscid_fromcatalog = get_sscid_fromIOCcatalog(group_id=347, groupname='uhslc')
    fname_fromcatalog = IOC_catalog_pd.loc[sscid_fromcatalog,'station_name_fname']
    """
    group_id = str(group_id)
    ssc_catalog_pd = ssc_ssh_read_catalog()
    _check_ssc_groups_valid(groupname)
        
    bool_strinseries = ssc_catalog_pd[groupname].apply(lambda x: group_id in x)
    if bool_strinseries.sum() < 1:
        raise ValueError('sscid not found for id %s in group %s'%(group_id, groupname))
    if bool_strinseries.sum() > 1:
        columns = ['name','country', 'geo:lat', 'geo:lon', groupname]
        subset = ssc_catalog_pd.loc[bool_strinseries, columns]
        raise ValueError(f'More than 1 sscid found for id {group_id} in group {groupname}:\n{subset}')
    
    sscid = ssc_catalog_pd.loc[bool_strinseries].index[0]
    return sscid


def ssc_ssh_subset_groups(groups, ssc_catalog_gpd=None):
    
    if ssc_catalog_gpd is None:
        ssc_catalog_gpd = ssc_ssh_read_catalog()
    
    if not isinstance(groups, list):
        groups = [groups]
    _check_ssc_groups_valid(groups)
    bool_ingroup = ssc_catalog_gpd[groups].apply(lambda x: x.str.len()).sum(axis=1)!=0
    ssc_catalog_gpd = ssc_catalog_gpd[bool_ingroup]

    return ssc_catalog_gpd


def ssc_ssh_read_catalog(linked_stations=False):
    """
    The SSC catalog contains e.g. UHSLC and GLOSS ids, this can be used
    for unique station naming across several observation datasets

    info: https://www.ioc-sealevelmonitoring.org/ssc.php
    station list: https://www.ioc-sealevelmonitoring.org/ssc/
    """
    
    #TODO: country column has 2-digit codes instead of 3
    url_json = 'https://www.ioc-sealevelmonitoring.org/ssc/service.php?format=json'
    ssc_catalog_pd = pd.read_json(url_json)
    
    #convert all cells with ids to list of strings or NaN
    for colname in ['psmsl','ioc','ptwc','gloss','uhslc','sonel_gps','sonel_tg']:
        ssc_catalog_pd[colname] = ssc_catalog_pd[colname].apply(lambda x: x if isinstance(x,list) else [] if x is np.nan else [x])
    
    # generate station_name_unique from name
    station_name_clean = ssc_catalog_pd['name']
    # remove accents
    station_name_clean = station_name_clean.apply(lambda x: _remove_accents(x))
    # replace all non-alphanumeric characters and with dash
    station_name_clean = station_name_clean.apply(lambda x: re.sub("[^0-9a-zA-Z]+", "-", x))
    # remove first/last dash from name if present
    station_name_clean = station_name_clean.str.strip('-')
    ssc_catalog_pd['station_name_unique'] = ssc_catalog_pd['ssc_id'] + '_' + station_name_clean
    
    #set ssc_id as index
    ssc_catalog_pd = ssc_catalog_pd.set_index('ssc_id',drop=False)
    
    # generate geom and geodataframe
    geom = [Point(x["geo:lon"], x["geo:lat"]) for irow, x in ssc_catalog_pd.iterrows()]
    ssc_catalog_gpd = gpd.GeoDataFrame(data=ssc_catalog_pd, geometry=geom, crs='EPSG:4326')
    ssc_catalog_gpd = ssc_catalog_gpd.drop(["geo:lon","geo:lat"], axis=1)
    
    rename_dict = {"name": "station_name", "ssc_id": "station_id"}
    ssc_catalog_gpd = ssc_catalog_gpd.rename(rename_dict, axis=1)
    
    if linked_stations:
        ssc_catalog_gpd = ssc_add_linked_stations(ssc_catalog_gpd)
    return ssc_catalog_gpd


def ssc_add_linked_stations(ssc_catalog_gpd):
    """
    Find xy-coordinates of UHSLC/IOC stations that are linked to the SSC stations,
    including the min/max distance between them.
    """
    # getting linked catalogs
    uhslc_catalog_gpd = _uhslc_get_json()
    # uhslc index is dtype int but we use loc with dtype str
    uhslc_catalog_gpd.index = uhslc_catalog_gpd.index.astype(str)
    ioc_catalog_gpd = _ioc_get_json(showall="all")
    # dropping duplicated code/geometry combinations
    ioc_catalog_gpd = ioc_catalog_gpd.drop_duplicates(['Code','geometry'])
    # ioc ids are not case sensitive, so convert entire IOC index to lowercase and 
    # convert linked_id to lowercase to avoid KeyError
    ioc_catalog_gpd.index = ioc_catalog_gpd.index.str.lower()

    linked_dict = {"ioc": ioc_catalog_gpd,
                   "uhslc": uhslc_catalog_gpd,
                   }
    for station_ssc_id, row in ssc_catalog_gpd.iterrows():
        x1 = row["geometry"].x
        y1 = row["geometry"].y
        
        station_check_dict = {}
        station_check_dist_all = []
        for linked_name, linked_gpd in linked_dict.items():
            for linked_id in row[linked_name]:
                linked_id = linked_id.lower()
                geom = linked_gpd.loc[linked_id]["geometry"]
                x2 = geom.x
                y2 = geom.y
                dist = ((x2-x1)**2 + (y2-y1)**2) **0.5
                station_check_dist_all.append(dist)
                station_key = f"{linked_name.upper()}: {linked_id}"
                station_check_dict[station_key] = [x2, y2, dist]
        
        if station_check_dict:
            # add additional columns if station_check_dict is not empty
            ssc_catalog_gpd.loc[station_ssc_id,'dist_dict'] = [station_check_dict]
            ssc_catalog_gpd.loc[station_ssc_id,'dist_min'] = np.min(station_check_dist_all)
            ssc_catalog_gpd.loc[station_ssc_id,'dist_max'] = np.max(station_check_dist_all)
    return ssc_catalog_gpd


def get_cmems_dataset_id(source):
    dataset_id_dict = {"cmems": "cmems_obs-ins_glo_phy-ssh_my_na_PT1H",
                       "cmems-nrt": "cmems_obs-ins_glo_phybgcwav_mynrt_na_irr"}
    dataset_id = dataset_id_dict[source]
    return dataset_id


def cmems_my_ssh_read_catalog(overwrite=True):
    cmems_catalog_gpd = cmems_ssh_read_catalog(source="cmems", overwrite=overwrite)
    return cmems_catalog_gpd
    

def cmems_nrt_ssh_read_catalog(overwrite=True):
    cmems_catalog_gpd = cmems_ssh_read_catalog(source="cmems-nrt", overwrite=overwrite)
    return cmems_catalog_gpd


def cmems_ssh_read_catalog(source, overwrite=True):
    dataset_id = get_cmems_dataset_id(source)
    
    dir_cache = get_dir_testdata()
    dir_index = os.path.join(dir_cache, dataset_id)
    os.makedirs(dir_index, exist_ok=True)
    file_index = os.path.join(dir_index, 'index_history.txt')
    
    if not os.path.exists(file_index) or overwrite:
        #TODO: downloading all index files since filter does not work, can be avoided?
        prev_lev = CM_LOGGER.level
        CM_LOGGER.setLevel("WARNING")
        copernicusmarine_credentials()
        copernicusmarine.get(
            dataset_id=dataset_id,
            index_parts=True,
            filter="*index_history.txt",
            output_directory=dir_index,
            overwrite=True,
            no_directories=True,
            disable_progress_bar=True,
            )
        CM_LOGGER.setLevel(prev_lev)
    else:
        print(f"CMEMS insitu catalog for dataset_id='{dataset_id}' is already present and overwrite=False")
    
    with open(file_index, 'r') as f:
        for line in f:
            if line.startswith('#'):
                header = line
            else:
                break #stop when there are no more #
    colnames = header.strip('#').strip().split(',')
    index_history_pd = pd.read_csv(file_index,comment='#',names=colnames)
    
    # filter only history tidegauges (TG) containing SLEV variable, relevant for nrt dataset
    # TODO: why are there non-SLEV files in TG folder? Cleanup possible?
    bool_tidegauge = index_history_pd["file_name"].str.contains("/history/TG/")
    bool_slev = index_history_pd["parameters"].str.contains("SLEV")
    index_history_pd = index_history_pd.loc[bool_tidegauge & bool_slev]
    
    # drop andratx station, lat/lon vary over time in nrt dataset
    # TODO: remove this exception when the CMEMS nrt dataset is cleaned up
    if source == "cmems-nrt":
        bool_moving = index_history_pd["file_name"].str.contains("MO_TS_TG_ANDRATX")
        index_history_pd = index_history_pd.loc[~bool_moving]
    
    # generate geom and geodataframe
    assert (index_history_pd["geospatial_lon_min"] == index_history_pd["geospatial_lon_max"]).all()
    assert (index_history_pd["geospatial_lat_min"] == index_history_pd["geospatial_lat_max"]).all()
    geom = [Point(x["geospatial_lon_min"], x["geospatial_lat_min"]) for irow, x in index_history_pd.iterrows()]
    index_history_gpd = gpd.GeoDataFrame(data=index_history_pd, geometry=geom, crs='EPSG:4326')
    drop_list = ["geospatial_lon_min", "geospatial_lon_max",
                 "geospatial_lat_min", "geospatial_lat_max"]
    index_history_gpd = index_history_gpd.drop(drop_list, axis=1)
    
    # add dummy country column for the test to pass
    # TODO: hopefully country metadata is available via cmems API in the future
    index_history_gpd["country"] = ""
    stat_ids = index_history_gpd["file_name"].apply(lambda x: os.path.basename(x).replace(".nc",""))
    stat_names = stat_ids.str.split("_").str[3] # corresponds to the cmems platform_code
    index_history_gpd["station_name"] = stat_names
    index_history_gpd["station_id"] = stat_names
    index_history_gpd["station_name_unique"] = stat_ids
    
    # rename columns
    rename_dict = {'time_coverage_start':'time_min',
                   'time_coverage_end':'time_max'}
    index_history_gpd = index_history_gpd.rename(rename_dict, axis=1)
    index_history_gpd["time_min"] = pd.to_datetime(index_history_gpd["time_min"])
    index_history_gpd["time_max"] = pd.to_datetime(index_history_gpd["time_max"])
    
    index_history_gpd["time_ndays"] = (index_history_gpd['time_max'] - index_history_gpd['time_min']).dt.total_seconds()/3600/24
    return index_history_gpd


def _uhslc_get_json():
    uhslc_gpd = gpd.read_file(
        "https://uhslc.soest.hawaii.edu/data/meta.geojson",
        engine="fiona"
        )
    
    for drop_col in ["rq_basin", "rq_versions"]:
        if drop_col in uhslc_gpd.columns:
            uhslc_gpd = uhslc_gpd.drop(drop_col, axis=1)
    
    uhslc_gpd = uhslc_gpd.set_index('uhslc_id', drop=False)
    
    # shift from 0to360 to -180to180, round off to avoid 134.463+180=314.46299999999997
    def _lon_360_to_180(lon):
        return round((lon + 180)%360 - 180, 10)
    from shapely import Point
    geom_shift = [Point(_lon_360_to_180(pnt.x), pnt.y) for pnt in uhslc_gpd.geometry]
    uhslc_gpd.geometry = geom_shift
    return uhslc_gpd


def uhslc_ssh_read_catalog():
    # TODO: country is "New Zealand" and country_code is 554. We would like country/country_code=NZL
    uhslc_gpd = _uhslc_get_json()
    
    time_min_rq = pd.to_datetime(uhslc_gpd["rq_span"].apply(lambda x: x["oldest"]))
    time_max_rq = pd.to_datetime(uhslc_gpd["rq_span"].apply(lambda x: x["latest"]))
    time_min_fd = pd.to_datetime(uhslc_gpd["fd_span"].apply(lambda x: x["oldest"]))
    time_max_fd = pd.to_datetime(uhslc_gpd["fd_span"].apply(lambda x: x["latest"]))
    # take the max of max and the min of min
    time_min = pd.concat([time_min_rq, time_min_fd], axis=1).min(axis=1)
    time_max = pd.concat([time_max_rq, time_max_fd], axis=1).max(axis=1)
    uhslc_gpd["time_min"] = time_min
    uhslc_gpd["time_max"] = time_max
    
    # remove accents from station names
    # https://github.com/Deltares/dfm_tools/issues/1172
    uhslc_gpd["name"] = uhslc_gpd["name"].apply(lambda x: _remove_accents(x))
    
    # define name/id columns
    stat_names = "uhslc-" + uhslc_gpd['uhslc_id'].apply(lambda x: f"{x:03d}")
    uhslc_gpd["station_name_unique"] = stat_names
    rename_dict = {"name": "station_name", "uhslc_id": "station_id"}
    uhslc_gpd = uhslc_gpd.rename(rename_dict, axis=1)
    
    time_diff = uhslc_gpd['time_max'] - uhslc_gpd['time_min']
    uhslc_gpd["time_ndays"] = time_diff.dt.total_seconds()/3600/24
    return uhslc_gpd


def _ioc_get_json(showall="a"):
    """
    howto available at https://www.ioc-sealevelmonitoring.org/service.php?query=help
    """
    url_json = f'https://www.ioc-sealevelmonitoring.org/service.php?query=stationlist&showall={showall}'
    resp = requests.get(url_json)
    if resp.status_code==404: #continue to next station if not found
        raise Exception(f'url 404: {resp.text}')
    resp_json = resp.json()
    ioc_catalog_pd = pd.DataFrame.from_dict(resp_json)
    
    #set ssc_id as index
    ioc_catalog_pd = ioc_catalog_pd.set_index('Code',drop=False)
    
    # generate geom and geodataframe and remove the old columns
    geom = [Point(x["lon"], x["lat"]) for irow, x in ioc_catalog_pd.iterrows()]
    ioc_catalog_gpd = gpd.GeoDataFrame(data=ioc_catalog_pd, geometry=geom, crs='EPSG:4326')
    drop_list = ["Lon","lat","lon","lat"]
    ioc_catalog_gpd = ioc_catalog_gpd.drop(drop_list, axis=1)
    return ioc_catalog_gpd


def ioc_ssh_read_catalog(drop_uhslc=True, drop_dart=True, drop_nonutc=True):
    """
    Generates a list of all active IOC stations (showall=a).
    showall=all is all known and returns more stations, but also returns nonexistent stations.
    The stations that are already in UHSLC are dropped,
    as well as DART stations and non-UTC stations.
    """
    #TODO: "Code" contains more station codes than "code", what is the difference?
    #TODO: "Location" contains full name, but contains spaces etcetera, retrieve from SSC instead?
    ioc_catalog_gpd = _ioc_get_json(showall="a")
    
    #derive start/stop times indications from metadata
    ioc_catalog_gpd["time_min"] = pd.to_datetime(ioc_catalog_gpd["date_created"])
    ioc_catalog_gpd["time_max"] = pd.to_datetime(ioc_catalog_gpd["lasttime"])
        
    ioc_catalog_gpd["station_name"] = ioc_catalog_gpd['Code']
    ioc_catalog_gpd["station_id"] = ioc_catalog_gpd['Code']
    stat_names = "ioc-" + ioc_catalog_gpd['Code'] + "-" + ioc_catalog_gpd['code'].astype(str)
    ioc_catalog_gpd["station_name_unique"] = stat_names

    if drop_uhslc:
        # filter non-UHSLC stations
        ssc_catalog_pd = ssh_catalog_subset(source='ssc')
        ioc_catalog_gpd['UHSLC'] = False
        ioc_catalog_gpd['inSSClist'] = False
        for ioc_code in ioc_catalog_gpd['Code']:
            iocstat_uhslcid = ssc_catalog_pd.loc[ssc_catalog_pd['ioc'].apply(lambda x: ioc_code in x),'uhslc']
            if len(iocstat_uhslcid)==0: #ioc_code not in the SSC list
                continue
            else:
                ioc_catalog_gpd.loc[ioc_code,'inSSClist'] = True
                if len(iocstat_uhslcid.iloc[0])==0: #station contains no UHSLC code
                    continue
                else:
                    ioc_catalog_gpd.loc[ioc_code,'UHSLC'] = True
        ioc_catalog_gpd = ioc_catalog_gpd.loc[~ioc_catalog_gpd['UHSLC']]
    
    if drop_dart:
        bool_dart = ioc_catalog_gpd["Location"].str.startswith("DART ")
        ioc_catalog_gpd = ioc_catalog_gpd.loc[~bool_dart]
    
    if drop_nonutc:
        # filter out all non-UTC stations
        ioc_catalog_gpd = ioc_catalog_gpd.loc[ioc_catalog_gpd['UTCOffset']==0]
    
    ioc_catalog_gpd["time_ndays"] = (ioc_catalog_gpd['time_max'] - ioc_catalog_gpd['time_min']).dt.total_seconds()/3600/24
    return ioc_catalog_gpd


def psmsl_gnssir_ssh_read_catalog():
    # https://psmsl.org/data/gnssir/metadatainfo.php
    # https://psmsl.org/data/gnssir/useful_files.php
    # url = "https://psmsl.org/data/gnssir/data/maplayers/good_sites.json"
    # TODO: use only good_sites instead of all (request field in json)
    # TODO: request time extents in json instead of with psmsl_gnssir_ssh_read_catalog_gettimes()
    url = "https://psmsl.org/data/gnssir/data/sites.json"
    station_list_pd = pd.read_json(url).T
    
    rename_dict = {"CountryCode":"country"}
    station_list_pd = station_list_pd.rename(rename_dict, axis=1)
    
    # generate geom and geodataframe and remove the old columns
    geom = [Point(x["Longitude"], x["Latitude"]) for irow, x in station_list_pd.iterrows()]
    station_list_gpd = gpd.GeoDataFrame(data=station_list_pd, geometry=geom, crs='EPSG:4326')
    drop_list = ["Longitude","Latitude"]
    station_list_gpd = station_list_gpd.drop(drop_list, axis=1)
    
    station_list_gpd['station_name'] = station_list_gpd['Code']
    station_list_gpd["station_id"] = station_list_gpd.index
    stat_names = "psmsl-gnssir-" + station_list_gpd['station_id'].astype(str) + "-" + station_list_gpd['station_name']
    station_list_gpd["station_name_unique"] = stat_names
    return station_list_gpd


def psmsl_gnssir_ssh_read_catalog_gettimes(station_list_gpd):
    # the catalog json does not contain time ranges so we derive it from daily csv files
    station_list_gpd["time_min"] = pd.NaT
    station_list_gpd["time_max"] = pd.NaT
    print(f"retrieving psmsl-gnssir time extents for {len(station_list_gpd)} stations:", end=" ")
    for station_id in station_list_gpd.index:
        irow = station_list_gpd.index.tolist().index(station_id)
        print(irow+1, end=" ")
        url = f"https://psmsl.org/data/gnssir/data/daily/{station_id}_daily.csv"
        data_daily = pd.read_csv(url)
        time_min = pd.Timestamp(data_daily["time"].iloc[0])
        time_max = pd.Timestamp(data_daily["time"].iloc[-1])
        station_list_gpd.loc[station_id, "time_min"] = time_min
        station_list_gpd.loc[station_id, "time_max"] = time_max
        station_list_gpd.loc[station_id, "time_ndays"] = (time_max - time_min).total_seconds()/3600/24
    print()

    return station_list_gpd

def gesla3_ssh_read_catalog(only_coastal=True):
    file_gesla3_meta = os.path.join(settings.PATH_GESLA3, "GESLA3_ALL 2.csv")
    
    if not os.path.isfile(file_gesla3_meta):
        raise FileNotFoundError(f"The 'file_gesla3_meta' file '{file_gesla3_meta}' was not found. "
                                "You can download it from https://gesla787883612.wordpress.com/downloads and provide the path")
    
    station_list_pd = pd.read_csv(file_gesla3_meta)
    station_list_pd.columns = [c.replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_").lower() for c in station_list_pd.columns]
    station_list_pd = station_list_pd.set_index('file_name', drop=False)
    station_list_pd["start_date_time"] = pd.to_datetime(station_list_pd["start_date_time"])
    station_list_pd["end_date_time"] = pd.to_datetime(station_list_pd["end_date_time"])
    
    # drop non-coastal
    if only_coastal:
        station_list_pd = station_list_pd.loc[station_list_pd['gauge_type']=='Coastal']
        
    # generate geom and geodataframe and remove the old columns
    geom = [Point(x["longitude"], x["latitude"]) for irow, x in station_list_pd.iterrows()]
    station_list_gpd = gpd.GeoDataFrame(data=station_list_pd, geometry=geom, crs='EPSG:4326')
    drop_list = ["longitude","latitude"]
    station_list_gpd = station_list_gpd.drop(drop_list, axis=1)
    
    stat_names = station_list_gpd["file_name"]
    station_list_gpd["station_name_unique"] = stat_names
    station_list_gpd["station_id"] = station_list_gpd["file_name"]
    station_list_gpd["station_name"] = station_list_gpd["site_name"]
    
    # rename columns
    rename_dict = {'start_date_time':'time_min',
                   'end_date_time':'time_max'}
    station_list_gpd = station_list_gpd.rename(rename_dict, axis=1)
    station_list_gpd["time_ndays"] = (station_list_gpd['time_max'] - station_list_gpd['time_min']).dt.total_seconds()/3600/24
    return station_list_gpd


def rwsddl_ssh_meta_dict():
    # combination for measured waterlevels
    meta_dict = {'Grootheid.Code':'WATHTE', 'Groepering.Code':'NVT'}
    return meta_dict


def rwsddl_ssh_get_time_max(locations):
    locations = locations.copy() # copy to prevent SettingWithCopyWarning
    
    print(f"getting time_max for {len(locations)} locations: ", end="")
    dtstart = pd.Timestamp.now()
    list_time_latest = []
    for stat_code, location in locations.iterrows():
        try:
            meas_latest = ddlpy.measurements_latest(location)
            time_latest = meas_latest.index.max().tz_convert(None)
        except ddlpy.ddlpy.NoDataException:
            time_latest = pd.NaT
        list_time_latest.append(time_latest)
    locations["time_max"] = list_time_latest
    print(f'{(pd.Timestamp.now()-dtstart).total_seconds():.2f} sec')
    return locations


def rwsddl_ssh_read_catalog(meta_dict=None):
    """
    convert LocatieLijst to geopandas dataframe
    """
    if meta_dict is None:
        meta_dict = rwsddl_ssh_meta_dict()
    
    locations = ddlpy.locations()
    selected = locations.copy()
    for key in meta_dict.keys():
        value = meta_dict[key]
        try:
            bool_sel = selected[key].isin([value])
            selected = selected.loc[bool_sel]
        except KeyError:
            cols_with_code = [x for x in selected.columns if ".Code" in x]
            raise KeyError(f"ddlpy.locations() cannot subset for '{key}', available are {cols_with_code}")            
    
    # add "Code" index as column and reset the index
    selected = selected.reset_index()
    
    xcoords = selected["X"]
    ycoords = selected["Y"]
    epsg_all = selected["Coordinatenstelsel"]
    epsg_uniq = epsg_all.unique()
    if len(epsg_uniq)>1:
        raise ValueError(f"multiple EPSG codes in one LocatieLijst not supported: {epsg_uniq.tolist()}")
    epsg = epsg_uniq[0]
    geom_points = [Point(x,y) for x,y in zip(xcoords,ycoords)]
    ddl_slev_gdf = gpd.GeoDataFrame(selected, geometry=geom_points, crs=epsg)
    
    # convert coordinates to wgs84
    ddl_slev_gdf = ddl_slev_gdf.to_crs(4326)
    
    ddl_slev_gdf["station_name"] = ddl_slev_gdf["Naam"]
    ddl_slev_gdf["station_name_unique"] = ddl_slev_gdf["Code"]
    ddl_slev_gdf["station_id"] = ddl_slev_gdf["Code"]
    ddl_slev_gdf["country"] = "NLD"
    
    return ddl_slev_gdf


def gtsm3_era5_cds_ssh_read_catalog():
    
    # There is no catalog file available via API, instead we load a text file containing
    # coordinates of gtsm3-era5 output points (that are available on CDS)
    url = ("https://raw.githubusercontent.com/VU-IVM/gtsm3-era5-nrt/refs/heads/main/"
           "04_supplementary_data/GTSM_output_locations_list.csv")
    station_list_pd = pd.read_csv(url,sep="\t")
    
    # generate geom and geodataframe and remove the old columns
    geom = [Point(x["lon"], x["lat"]) for irow, x in station_list_pd.iterrows()]
    station_list_gpd = gpd.GeoDataFrame(data=station_list_pd,
                                        geometry=geom,
                                        crs='EPSG:4326',
                                        )
    drop_list = ["lon","lat"]
    station_list_gpd = station_list_gpd.drop(drop_list, axis=1)
    
    stat_names = ("gtsm3-era5-" + station_list_gpd['station_id'].astype(str) +
                  "-" + station_list_gpd['station_name'])
    station_list_gpd["station_name_unique"] = stat_names
    station_list_gpd["country"] = ""
    time_start = pd.Timestamp('1950-01-01')
    time_end = pd.Timestamp('2024-12-31')
    station_list_gpd["time_min"] = time_start
    station_list_gpd["time_max"] = time_end
    station_list_gpd["time_ndays"] = (time_end - time_start).total_seconds()/3600/24

    return station_list_gpd  


def cmems_ssh_retrieve_data(row, time_min=None, time_max=None,
                            level="WARNING"):
    """
    Retrieve data from copernicusmarine files service
    Can only retrieve entire files, subsetting is done during reconstruction
    
    Sometimes the process hangs when using disable_progress_bar=True
    """
    
    # get source from gdf
    source = row['source']
    dataset_id = get_cmems_dataset_id(source)

    tempdir = tempfile.gettempdir()
    url_file = row["file_name"]
    
    prev_lev = CM_LOGGER.level
    CM_LOGGER.setLevel(level)
    copernicusmarine_credentials()
    copernicusmarine.get(
        dataset_id=dataset_id,
        dataset_part="history",
        filter=url_file,
        output_directory=tempdir,
        overwrite=True,
        no_directories=True,
        disable_progress_bar=True,
        )
    CM_LOGGER.setLevel(prev_lev)
    
    file_data_org = os.path.join(tempdir, os.path.basename(url_file))
    random_suffix = str(pd.Timestamp.now().microsecond)[0]
    file_data_raw = os.path.join(tempdir, f"dfmtools_cmems_ssh_retrieve_data_temporary_file_{random_suffix}.nc")
    if os.path.exists(file_data_raw):
        os.remove(file_data_raw)
    os.rename(file_data_org, file_data_raw)
    
    # reconstruct this dataset (including time subsetting) and write again
    ds = xr.open_dataset(file_data_raw)
    
    # reduce DEPTH dimension if present (nrt dataset)
    if "DEPTH" in ds.dims:
        ds = ds.max(dim="DEPTH", keep_attrs=True)

    ds = ds.rename(TIME="time")
    
    # keep only data with "good_data" quality flag
    ds["SLEV"] = ds.SLEV.where(ds.SLEV_QC==1)
    ds = ds.rename_vars(SLEV="waterlevel")
    
    # drop all unnecessary vars
    ds = ds.drop_vars(["SLEV_QC","TIME_QC","DEPH_QC","LATITUDE","LONGITUDE","STATION","POSITION"], errors="ignore")
    
    # slice on time extent
    ds = ds.sel(time=slice(time_min, time_max))
    if len(ds.time) == 0:
        print("[NODATA] ", end="")
        del ds
        os.remove(file_data_raw)
        return
    
    return ds


def _preprocess_uhslc_erddap(ds):
    # drop rowSize before merging to avoid conflicts
    ds = ds.drop_vars("rowSize")
    
    # dropping all geospatial vars/attrs since they are not always consistent
    # UHSLC_ID=7 has ds_rqds.latitude=7.333 and ds_fast.latitude=7.33
    # UHSLC_ID=9 has geospatial_lat_min=-9.425 and geospatial_lat_max=-9.421
    # more info: https://github.com/Deltares/dfm_tools/issues/1192
    lon_attrs = ["geospatial_lon_min", "geospatial_lon_max", "Easternmost_Easting", "Westernmost_Easting"]
    lat_attrs = ["geospatial_lat_min", "geospatial_lat_max", "Northernmost_Northing", "Southernmost_Northing"]
    for attr in lon_attrs+lat_attrs:
        ds.attrs.pop(attr, None)
    ds = ds.drop_vars(["latitude", "longitude"], errors='ignore')
    
    # drop station_name since it conflicts between rqds and fast uhslc_id 53/108/more
    ds = ds.drop_vars(["station_name"], errors='ignore')

    # reduce dataset size by reducing all constant variables defined for each timestep
    constant_vars = ['station_country', 'station_country_code', 'gloss_id', 'ssc_id', 'last_rq_date']
    reduce_vars = set(ds.variables).intersection(constant_vars)
    logging.debug(f"UHSLC reducing variables {reduce_vars}")
    for var in reduce_vars:
        ds[var] = ds[var].max(dim='obs')
    
    # reduce timeseries dimension
    assert ds.sizes["timeseries"] == 1
    ds = ds.max(dim="timeseries", keep_attrs=True)
    
    # change units to meters
    with xr.set_options(keep_attrs=True):
        assert ds["sea_level"].attrs["units"] == "millimeters"
        ds["sea_level"] = ds["sea_level"]/1000
        ds["sea_level"] = ds["sea_level"].assign_attrs(units="m")
    ds = ds.rename_vars(sea_level="waterlevel")
    
    # set time index
    ds = ds.set_index(obs="time").rename(obs="time")
    # round times to seconds, should probably not be necessary
    ds['time'] = ds.time.dt.round('s')
    
    # rename some important conflicting attributes to avoid dropping by xr.concat
    dsid = ds.attrs["id"] # global_hourly_rqds/global_hourly_fast
    dsid_short = dsid.split("_")[-1] # rqds/fast
    for attr in ["acknowledgement", "processing_level", "title"]:
        ds.attrs[f"{attr}_{dsid_short}"] = ds.attrs.pop(attr)

    return ds


def _uhslc_ssh_retrieve_data_oneds(e, ds_list):
    """
    retrieve rqds/fast dataset by calling ERDDAP.to_xarray(). If this succeeds the
    dataset is preprocessed and appended to ds_list.
    
    It is also possible that the request fails with a HTTPError. In that case
    only raise a HTTPError with code!=404, so at least two errors are filtered/accepted
    and let the code continue without breaking.
    
    *** httpx.HTTPError: Error {
        code=404;
        message="Not Found: Your query produced no matching results. (nRows = 0)";
    }
    
    *** httpx.HTTPError: Error {
        code=404;
        message="Not Found: Your query produced no matching results. 
        (time>=3000-01-01T00:00:00Z is outside of the variable's actual_range: 
         1846-01-04T00:00:00Z to 2023-12-31T22:59:59Z)";
    }
    
    Any other HTTPErrors like timeouts (504) or outages (503), are still raised.
    This also goes for any other Exception.
    
    In all error-cases, nothing is appended to ds_list.
    """
    from httpx import HTTPError
    try:
        ds = e.to_xarray()
        ds = _preprocess_uhslc_erddap(ds)
        ds_list.append(ds)
    except HTTPError as err:
        if not "code=404" in str(err):
            raise


def uhslc_ssh_retrieve_data(row, time_min=None, time_max=None, include_rqds=True, include_fast=True):
    # docs from https://ioos.github.io/erddapy/ and https://ioos.github.io/erddapy/02-extras-output.html#
    
    # setup server connection, this takes no time so does not have to be cached
    # opendap is way slower than nc/csv/html
    server = "https://uhslc.soest.hawaii.edu/erddap"
    e = ERDDAP(server=server, protocol="tabledap", response="nc")
    
    # set erddap constraints
    e.constraints = {}
    uhslc_id = row.name
    e.constraints["uhslc_id="] = uhslc_id
    if time_min is not None:
        e.constraints["time>="] = pd.Timestamp(time_min)
    if time_max is not None:
        e.constraints["time<="] = pd.Timestamp(time_max)
    
    ds_list = []
    if include_rqds:
        e.dataset_id = "global_hourly_rqds"
        _uhslc_ssh_retrieve_data_oneds(e, ds_list)
    if include_fast:
        e.dataset_id = "global_hourly_fast"
        _uhslc_ssh_retrieve_data_oneds(e, ds_list)
    
    # return early if no data present
    if len(ds_list) == 0:
        return
    
    # merge/concat: coords/data_vars/compat/combine_attrs make sure errors are raised
    # if the datasets are not equal enough. All inequalities should be resolved by
    # _preprocess_uhslc_erddap() so there should be no error.
    ds = xr.concat(
        ds_list,
        dim='time',
        coords='minimal',
        data_vars='minimal',
        compat='equals',
        combine_attrs='drop_conflicts',
        )
    
    # drop duplicates, keep=first keeps rqds, sort by time
    ds = ds.drop_duplicates(dim='time', keep='first').sortby('time')
    return ds


@functools.lru_cache
def gesla3_cache_zipfile():
    file_gesla3_data = os.path.join(settings.PATH_GESLA3, "GESLA3.0_ALL.zip")

    if not os.path.isfile(file_gesla3_data):
        raise FileNotFoundError(
            f"The 'file_gesla3_data' file '{file_gesla3_data}' was not found. You can "
            "download it from https://gesla787883612.wordpress.com/downloads and provide "
            "the path: `import dfm_tools as dfmt; dfmt.settings.PATH_GESLA3 = 'path/to/gesla3'`"
            )
    
    gesla3_zip = ZipFile(file_gesla3_data)
    return gesla3_zip


def gesla3_ssh_retrieve_data(row, time_min=None, time_max=None):
    
    # get cached gesla3 zipfile instance
    gesla3_zip = gesla3_cache_zipfile()
    
    file_gesla = row.name
    with gesla3_zip.open(file_gesla, "r") as f:
        data = pd.read_csv(f, comment='#', sep="\\s+",
                           names=["date", "time", "sea_level", "qc_flag", "use_flag"],
                           )
    # set datetimes as index
    dates = data.pop("date")
    times = data.pop("time")
    data_dt = dates + " " + times
    data.index = pd.to_datetime(data_dt)
    
    # clean up time duplicates
    data.index.name = 'time'
    bool_duplicate = data.index.duplicated()
    if bool_duplicate.sum() > 0:
        data = data.loc[~bool_duplicate]
        print(f"[{bool_duplicate.sum()} duplicate timestamps removed] ", end="")
    
    # convert to xarray and add metadata
    ds = data.to_xarray()
    ds['sea_level'] = ds['sea_level'].assign_attrs(units="m")
    ds = ds.rename_vars(sea_level="waterlevel")
    
    # subset time
    ds = ds.sel(time=slice(time_min, time_max))
    if len(ds.time) == 0:
        return
    
    # filter bad quality data
    ds = ds.where(ds.qc_flag==1)
    return ds


def ioc_ssh_retrieve_data(row, time_min, time_max, subset_hourly=False):
    
    # https://www.ioc-sealevelmonitoring.org/service.php?query=help
    
    station_code = row.name
    period_range = pd.period_range(time_min, time_max, freq="1M")
    
    if time_min is None or time_max is None:
        raise ValueError("cannot supply None for 'time_min' or 'time_max' to 'ioc_ssh_retrieve_data()'")
    
    results_list = []
    for date in period_range:
        print('.', end="")
        year = date.year
        month = date.month
        starttime = date.to_timestamp()
        if month==12:
            endtime = pd.Timestamp(year+1,1,1)
        else:
            endtime = pd.Timestamp(year,month+1,1)
        url_json = (f"https://www.ioc-sealevelmonitoring.org/service.php?query=data&"
                    f"code={station_code}&format=json&"
                    f"timestart={starttime.isoformat()}&timestop={endtime.isoformat()}")
        resp = requests.get(url_json)
        if resp.status_code==404: #continue to next station if not found
            raise Exception(f'url 404: {resp.text}')    
        if resp.text == '[]':
            print("[NODATA]", end="")
            continue
        resp_json = resp.json()
        if 'error' in resp.json()[0].keys():
            raise Exception(resp.text)
        data_pd_one = pd.DataFrame.from_dict(resp_json)
        results_list.append(data_pd_one)
    print(" ", end="")
    
    if len(results_list)==0:
        # continue with next station if no data present in entire period
        return
    
    # convert to xarray
    data_pd_all = pd.concat(results_list)
    data_pd_all = data_pd_all.rename({"stime":"time"},axis=1)
    data_pd_all = pd.DataFrame({'slevel':data_pd_all['slevel'].values},
                            index=pd.to_datetime(data_pd_all['time']))
    data_pd_all = data_pd_all[~data_pd_all.index.duplicated(keep='last')]
    if subset_hourly:
        data_pd_all = data_pd_all.loc[data_pd_all.index.minute==0]
    ds = data_pd_all.to_xarray()
    ds = ds.rename_vars(slevel="waterlevel")
    ds["waterlevel"] = ds["waterlevel"].assign_attrs({"units":"m"})
    return ds


def psmsl_gnssir_ssh_retrieve_data(row, time_min=None, time_max=None):
    
    # https://psmsl.org/data/gnssir/gnssir_daily_means.html
    # https://psmsl.org/data/gnssir/gnssir_example.html (also contains IOC retrieval example)
    
    station_id = row.name
    resp = urlopen(rf"https://psmsl.org/data/gnssir/data/main/{station_id}.zip")
    myzip = ZipFile(BytesIO(resp.read()))
    with myzip.open(f"{station_id}.csv") as f:
        data = pd.read_csv(f, comment="#", parse_dates=["time"])
    
    url = f"https://psmsl.org/data/gnssir/data/sites/{station_id}.json"
    station_meta = pd.read_json(url)["properties"]
    #TODO: the below equation is a guess to get the reference level about right
    data['slev'] = data['adjusted_height'] - station_meta['ellipsoidalHeight'] + station_meta['reflectorHeight']
    data = data.set_index("time")
    
    ds = data.to_xarray()
    ds['slev'] = ds['slev'].assign_attrs(units="m")
    ds = ds.rename_vars(slev="waterlevel")

    ds = ds.sel(time=slice(time_min, time_max))
    if len(ds.time) == 0:
        return
    return ds


def rwsddl_ssh_retrieve_data(row, time_min, time_max):
    
    if time_min is None or time_max is None:
        raise ValueError("cannot supply None for 'time_min' or 'time_max' to 'rwsddl_ssh_retrieve_data()'")
    
    # if we pass one row to the measurements function you can get all the measurements
    measurements = ddlpy.measurements(row, time_min, time_max)
    
    if measurements.empty:
        # no output so this station is skipped
        return
    
    # minimize disk usage of StatuswaardeLijst by converting to U1
    varn_status = "WaarnemingMetadata.StatuswaardeLijst"
    status_dict = {"O":"Ongecontroleerd",
                   "G":"Gecontroleerd",
                   "D":"Definitief"}
    for k,v in status_dict.items():
        measurements[varn_status] = measurements[varn_status].str.replace(v, k)
    
    # convert to xarray (dropping some constant columns)
    drop_if_constant = ["WaarnemingMetadata.OpdrachtgevendeInstantieLijst",
                        "WaarnemingMetadata.BemonsteringshoogteLijst",
                        "WaarnemingMetadata.ReferentievlakLijst",
                        "AquoMetadata_MessageID",
                        "BioTaxonType", 
                        "BemonsteringsSoort.Code",
                        "Compartiment.Code",
                        "Eenheid.Code",
                        "Grootheid.Code",
                        "Hoedanigheid.Code",
                        "WaardeBepalingsmethode.Code",
                        "MeetApparaat.Code",
                        ]
    ds = ddlpy.dataframe_to_xarray(measurements, drop_if_constant)
    
    ds[varn_status] = ds[varn_status].assign_attrs(status_dict)
    
    rename_dict = {'Meetwaarde.Waarde_Numeriek':'waterlevel',
                   'WaarnemingMetadata.KwaliteitswaardecodeLijst':'qc',
                   'WaarnemingMetadata.StatuswaardeLijst':'status'}
    ds = ds.rename_vars(rename_dict)
    
    # convert meters to cm
    eenheid = ds.attrs.pop('Eenheid.Code')
    if eenheid != 'cm':
        raise Exception("unexpected unit")
    ds['waterlevel'] = ds['waterlevel'].assign_attrs(units="m")
    ds['waterlevel'] /= 100 #convert from cm to m
    return ds


def gtsm3_era5_cds_ssh_retrieve_data(row,
                                     time_min=None,
                                     time_max=None,
                                     time_freq='10_min',
                                     ):
    """
    Retrieve data from Climate Data Store. Can only retrieve entire files, subsetting
    for time and stations is done after download. The function checks if the files have
    already been downloaded to cache.
    """
    
    if time_min is None:
        time_min = row['time_min']
    if time_max is None:
        time_max = row['time_max']

    dir_cache = get_dir_testdata()
    dir_cache_gtsm = os.path.join(dir_cache,'gtsm3_era5_cds')
    os.makedirs(dir_cache_gtsm, exist_ok=True)

    # Get a list of all monthly time periods within the time range
    time_periods = pd.period_range(start=time_min, end=time_max, freq='M')
    
    time_freq_cds_str = time_freq.replace("_","")
    file_pat = os.path.join(dir_cache_gtsm,
                            f'reanalysis_waterlevel_{time_freq_cds_str}_*_v2.nc',
                            )
    # Retrieve data via an API request and extract archive (if not found in the cache)
    for period in time_periods:
        period_cds_str = str(period).replace("-","_")
        filename = file_pat.replace("*", period_cds_str)
        if os.path.isfile(filename):
            continue

        print(f'retrieving GTSM3-ERA5-CDS data for {period}')
        tmp_zipfile = filename.replace(".nc",".zip")

        if time_freq not in ['10_min', 'hourly']:
            raise ValueError(
                "time frequency for retrieving gtsm3-era5-cds data should be one of "
                f"['10_min','hourly'], received '{time_freq}'")

        # prompt for CDS credentials if /.cdsapirc file is not present
        cds_credentials()
        # Make connection with CDS via API
        c = cdsapi.Client() 
        c.retrieve(
            'sis-water-level-change-timeseries-cmip6',
            {
                'variable': "total_water_level",
                'experiment': 'reanalysis',
                'temporal_aggregation': time_freq,
                'year': str(period.year),
                'month': str(period.month).zfill(2),
                'format': 'zip',
            }, 
            tmp_zipfile)
    
        with ZipFile(tmp_zipfile, 'r') as zip_ref:
            zip_ref.extractall(dir_cache_gtsm)
        os.remove(tmp_zipfile)

    # open dataset
    ds = xr.open_mfdataset(file_pat, data_vars='minimal', join='exact')
    
    # slice on time extent
    ds = ds.sel(time=slice(time_min, time_max))

    # subset stations
    ds_station = ds.sel(stations=row['station_id']); ds.close()
    ds_station = ds_station.drop_vars({'station_x_coordinate',
                                       'station_y_coordinate',
                                       'stations'})
    return ds_station

  
def ssh_catalog_subset(source=None,
                       lon_min=-180, lon_max=180, 
                       lat_min=-90, lat_max=90, 
                       time_min=None, time_max=None,
                       **kwargs):
    # TODO: check if min<max
    # TODO: accept None but replace with min/max value from dict. Time min/max are pd.min() and pd.max()
    # TODO: accept partial None (now one None is same as all None)
    
    ssh_sources = {"ssc": ssc_ssh_read_catalog,
                   "gesla3": gesla3_ssh_read_catalog,
                   "ioc": ioc_ssh_read_catalog,
                   "cmems": cmems_my_ssh_read_catalog,
                   "cmems-nrt": cmems_nrt_ssh_read_catalog,
                   "uhslc": uhslc_ssh_read_catalog,
                   "psmsl-gnssir": psmsl_gnssir_ssh_read_catalog,
                   "rwsddl": rwsddl_ssh_read_catalog,
                   "gtsm3-era5-cds": gtsm3_era5_cds_ssh_read_catalog,
                   }
    
    if source not in ssh_sources.keys():
        raise ValueError(f"source for ssh_catalog_subset should be one of {list(ssh_sources.keys())}, recieved '{source}'")
    
    catalog_read_func = ssh_sources[source]
    
    ssh_catalog_gpd = catalog_read_func(**kwargs)
    ssh_catalog_gpd["source"] = source

    # spatial subsetting and sort again: https://github.com/geopandas/geopandas/issues/2937
    ssh_catalog_gpd = ssh_catalog_gpd.clip((lon_min, lat_min, lon_max, lat_max))
    ssh_catalog_gpd = ssh_catalog_gpd.sort_index()
    
    if None not in [time_min, time_max]:
        if source=="psmsl-gnssir":
            ssh_catalog_gpd = psmsl_gnssir_ssh_read_catalog_gettimes(ssh_catalog_gpd)
        if "time_min" not in ssh_catalog_gpd.columns:
            raise KeyError(f"ssh_catalog_gpd for source='{source}' does not contain time_min and time_max, no time subsetting possible.")
        intime_bool = ((ssh_catalog_gpd['time_min']<time_max) &
                       (ssh_catalog_gpd['time_max']>time_min)
                       )
        ssh_catalog_gpd = ssh_catalog_gpd.loc[intime_bool].copy()
    return ssh_catalog_gpd


def ssh_retrieve_data(ssh_catalog_gpd, dir_output, time_min=None, time_max=None,
                      **kwargs):
    
    if ssh_catalog_gpd.empty:
        raise ValueError("empty ssh_catalog_gpd provided to ssh_retrieve_data")
    
    source_list = ssh_catalog_gpd["source"].unique()
    if len(source_list) > 1:
        raise Exception("A ssh_catalog_gpd with multiple unique 'source' values was passed to ssh_retrieve_data(), this is not supported.")
    source = source_list[0]
    
    ssh_sources = {"gesla3": gesla3_ssh_retrieve_data,
                   "ioc": ioc_ssh_retrieve_data,
                   "cmems": cmems_ssh_retrieve_data,
                   "cmems-nrt": cmems_ssh_retrieve_data,
                   "uhslc": uhslc_ssh_retrieve_data,
                   "psmsl-gnssir": psmsl_gnssir_ssh_retrieve_data,
                   "rwsddl": rwsddl_ssh_retrieve_data,
                   "gtsm3-era5-cds": gtsm3_era5_cds_ssh_retrieve_data,
                   }
    
    if source not in ssh_sources.keys():
        raise ValueError(f"source for ssh_retrieve_data should be one of {list(ssh_sources.keys())}, recieved '{source}'")
    
    retrieve_data_func = ssh_sources[source]
    os.makedirs(dir_output, exist_ok=True)
    
    # retrieve
    print(f"retrieving data for {len(ssh_catalog_gpd)} {source} stations:", end=" ")
    for idx_arbitrary, row in ssh_catalog_gpd.iterrows():
        irow = ssh_catalog_gpd.index.tolist().index(idx_arbitrary)
        print(irow+1, end=" ")
        ds = retrieve_data_func(row, time_min=time_min, time_max=time_max, **kwargs)
        if ds is None:
            print("[NODATA] ",end="")
            continue
        
        # assign attrs from station catalog row
        ds = ds.assign_attrs(station_name=row["station_name"],
                             station_id=row["station_id"],
                             station_name_unique=row["station_name_unique"],
                             longitude=row.geometry.x,
                             latitude=row.geometry.y,
                             country=row["country"],
                             source=row["source"])
        
        ds["waterlevel"] = ds["waterlevel"].astype("float32")
        ds = _make_hydrotools_consistent(ds)
        
        stat_name = ds.attrs["station_name_unique"]
        file_out = os.path.join(dir_output, f"{stat_name}.nc")
        # format NETCDF4_CLASSIC significantly reduces netcdf file size
        # in case of <U1 and <U2 variables
        ds.to_netcdf(file_out, format="NETCDF4_CLASSIC")
        del ds
    print()


def ssh_catalog_toxynfile(ssc_catalog_gpd, file_xyn):
    """
    converts a ssh_catalog to a _obs.xyn file for easy visualisation in FM GUI and interacter
    """
    lon = ssc_catalog_gpd.geometry.x
    lat = ssc_catalog_gpd.geometry.y
    name = ssc_catalog_gpd['station_name_unique']
    data = np.c_[lon, lat, name]
    np.savetxt(file_xyn, data, fmt='%13.8f %13.8f %-s')


def ssh_catalog_tokmlfile(ssc_catalog_gpd, file_kml):
    """
    converts a ssh_catalog to a kml file for easy visualisation in google earth
    """
    #select only names and geometry column to reduce file size
    ssc_catalog_gpd_minimal = ssc_catalog_gpd[["station_name_unique","geometry"]]
    # rename to "name" results in a label in Google Earth
    ssc_catalog_gpd_minimal = ssc_catalog_gpd_minimal.rename({"station_name_unique":"name"},axis=1)
    
    # Write file
    ssc_catalog_gpd_minimal.to_file(file_kml, driver='KML')


def ssh_netcdf_overview(dir_netcdf, perplot=30, time_min=None, time_max=None, yearstep=None):
    """
    reads all netcdf files in a directory and makes figures of the non-nan waterlevel time availability
    it also writes a csv file with statistics
    """
    dir_output = os.path.join(dir_netcdf, "overview")
    if os.path.isdir(dir_output):
        shutil.rmtree(dir_output)
    os.makedirs(dir_output, exist_ok=False)
    
    file_list = glob.glob(os.path.join(dir_netcdf, "*.nc"))
    file_list = sorted(file_list, key=str.casefold)
    
    print(f"creating overview for {len(file_list)} files: ", end="")
    fig, ax = plt.subplots(figsize=(15,8))
    stats_list = []
    fig_file_list = []
    for ifile, file_nc in enumerate(file_list):
        
        fname = os.path.basename(file_nc)
        print(f"{ifile+1} ", end="")
        
        ds = xr.open_dataset(file_nc)
        ds = ds.sortby("time") # necessary for BODC data
        
        fname_clean = fname.replace(".nc","")
        longitude = float(ds.station_x_coordinate)
        latitude = float(ds.station_y_coordinate)
        
        fig_file_list.append(fname_clean)
        
        # stats
        ds_ndays = round(int(ds.time.max() - ds.time.min())/1e9/3600/24, 2)
        nvalues = len(ds.waterlevel)
        nnan = int(ds.waterlevel.isnull().sum())
        time_diff_min = ds.time.to_pandas().diff().dt.total_seconds()/60
        
        stats_one = {"fname_clean":fname_clean,
                     "longitude":longitude,
                     "latitude":latitude,
                     "tstart":str(ds.time[0].dt.strftime("%Y-%m-%d").values),
                     "tstop":str(ds.time[-1].dt.strftime("%Y-%m-%d").values),
                     "ndays": ds_ndays,
                     "#values": nvalues,
                     "#nan": nnan,
                     "%nan": (nnan/nvalues)*100,
                     "dt min [min]":int(time_diff_min.min()),
                     "dt max [min]":int(time_diff_min.max()),
                     "dt mean [min]":time_diff_min.mean(),
                     "dt mode [min]":time_diff_min.mode().iloc[0],
                     "ndupl": ds.time.to_pandas().duplicated().sum(),
                     "min": float(ds.waterlevel.min()),
                     "max": float(ds.waterlevel.max()),
                     }
        stats_one_pd = pd.DataFrame(stats_one, index=[fname])
        stats_list.append(stats_one_pd)
        
        # derive unique hourly times with non-nan values
        bool_nan = ds.waterlevel.isnull()
        ds_nonan = ds.sel(time=~bool_nan)
        ds_slice = ds_nonan.sel(time=slice(time_min, time_max))
        # take unique timestamps after rounding to hours, this is faster and consumes less memory
        time_hr_uniq = ds_slice.time.to_pandas().index.round("h").drop_duplicates()
        time_yaxis_value = pd.Series(index=time_hr_uniq)
        time_yaxis_value[:] = -(ifile%perplot)
        time_yaxis_value.plot(ax=ax, marker='s', linestyle='none', markersize=1, color="r")
        
        # clear file links
        del ds
        
        bool_lastinrange = (ifile%perplot) == (perplot-1)
        bool_lastfile = ifile == (len(file_list)-1)
        if bool_lastinrange | bool_lastfile:
            # finish and save figure
            nlines = len(fig_file_list)
            ax.set_yticks(range(0,-nlines,-1), fig_file_list)
            figname = f"overview_availability_{ifile-nlines+2:03d}_{ifile+1:03d}"
            ax.set_xlim(time_min, time_max)
            if yearstep is not None:
                # set xtick steps
                ax.xaxis.set_major_locator(md.YearLocator(base=yearstep))
                ax.xaxis.set_major_formatter(md.DateFormatter('%Y'))
            ax.grid()
            ax.set_xlabel(None)
            fig.tight_layout()
            fig.savefig(os.path.join(dir_output, figname), dpi=200)
        
        if bool_lastinrange:
            # reset figure
            ax.cla()
            fig_file_list = []
    print()
    
    # write statistics csv
    stats = pd.concat(stats_list)
    stats.index.name = "file_name"
    file_csv = os.path.join(dir_output, "waterlevel_data_netcdf_overview.csv")
    stats.to_csv(file_csv, float_format="%.2f")
    
