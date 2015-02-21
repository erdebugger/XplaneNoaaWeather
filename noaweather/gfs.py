import threading
from datetime import datetime, timedelta
import os
import subprocess

from wafs import WAFS
from metar import Metar
from asyncdownload import AsyncDownload
from c import c
import sys



class GFS(threading.Thread):
    '''
    NOAA GFS download and parse functions.
    '''
    cycles = [0, 6, 12, 18]
    baseurl = 'http://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p50.pl?'
    
    params = [
              'leftlon=0',
              'rightlon=360',
              'toplat=90',
              'bottomlat=-90',
              ]
    levels  = [
              '700_mb', # FL100
              '600_mb', # FL140
              '500_mb', # FL180
              '400_mb', # FL235
              '300_mb', # FL300
              '200_mb', # FL380
              '150_mb', # FL443
              '100_mb', # FL518
              'high_cloud_bottom_level',
              'high_cloud_layer',
              'high_cloud_top_level',
              'low_cloud_bottom_level',
              'low_cloud_layer',
              'low_cloud_top_level',
              'mean_sea_level',
              'middle_cloud_bottom_level',
              'middle_cloud_layer',
              'middle_cloud_top_level',
              #'surface',
               ]
    variables = ['PRES',
                 'TCDC',
                 'UGRD',
                 'VGRD',
                 'TMP',
                 'PRMSL',
                 #'RH',
                 ]
    nwinds, nclouds = 0, 0
    
    downloading = False
    downloadWait = 0
    # wait n seconds to start download
    lastgrib    = False
    
    lat, lon, lastlat, lastlon = False, False, False, False
    
    cycle = ''
    lastcycle = ''
    
    winds  = False
    clouds = False
    pressure = False
    newGrib = False
    parsed_latlon = (0, 0)
    
    die = threading.Event()
    dummy = threading.Event()
    lock = threading.Lock()
    
    def __init__(self, conf):
        self.conf = conf
        self.lastgrib = self.conf.lastgrib
        self.wafs = WAFS(conf, self.lock)
        self.metar = Metar(conf, self.lock)
        threading.Thread.__init__(self)
    
    def run(self):
        # Worker thread
        while not self.die.wait(self.conf.parserate):
            
            datecycle, cycle, forecast = self.getCycleDate()

            if self.downloadWait < 1:
                self.downloadCycle(datecycle, cycle, forecast)
            elif self.downloadWait > 0:
                self.downloadWait -= self.conf.parserate
            
            # Run WAFS worker
            self.wafs.run(self.lat, self.lon, self.conf.parserate)
            
            # Run Metar worker
            self.metar.run(self.lat, self.lon, self.conf.parserate)
            
            if self.die.isSet():
                # Kill downloaders if avaliable
                if self.wafs and self.wafs.downloading and self.wafs.download:
                    self.wafs.download.die()
                if self.downloading and self.download:
                    self.download.die()
                return
            
            # flush stdout 
            sys.stdout.flush()
        
    def getCycleDate(self):
        '''
        Returns last cycle date avaliable
        '''
        now = datetime.utcnow() 
        #cycle is published with 4 hours 25min delay
        cnow = now - timedelta(hours=4, minutes=0)
        #get last cycle
        for cycle in self.cycles:
            if cnow.hour >= cycle:
                lcycle = cycle
        # Forecast
        adjs = 0
        if cnow.day != now.day:
            adjs = +24
        forecast = (adjs + now.hour - lcycle)/3*3

        return ( '%d%02d%02d%02d' % (cnow.year, cnow.month, cnow.day, lcycle), lcycle, forecast)

    def downloadCycle(self, datecycle, cycle, forecast):
        '''
        Downloads the requested grib file
        '''
        
        filename = 'gfs.t%02dz.pgrb2full.0p50.f0%02d' % (cycle, forecast)
        
        path = os.sep.join([self.conf.cachepath, datecycle]) 
        cachefile = os.sep.join([datecycle, filename])  + '.grib2'
        
        if cachefile == self.lastgrib:
            # No need to download
            return
        
        if not os.path.exists(path):
            os.makedirs(path)
        
        if self.downloading == True:
            if not self.download.q.empty():
            
                #Finished downloading
                lastgrib = self.download.q.get()
                
                # Dowload success
                if lastgrib:
                    self.lock.acquire()
                    self.lastgrib = lastgrib
                    self.conf.lastgrib = self.lastgrib
                    self.newGrib = True
                    #print "new grib file: " + self.lastgrib
                    self.lock.release()
                else:
                    # Wait a minute
                    self.downloadWait = 60
                    
                self.downloading = False
                
        elif self.conf.download and self.downloadWait < 1:
            # Download new grib
            
            ## Build download url
            params = self.params;
            dir =  'dir=%%2Fgfs.%s' % (datecycle)
            params.append(dir)
            params.append('file=' + filename)  
            
            # add variables
            for level in self.levels:
                params.append('lev_' + level + '=1')
            for var in self.variables:
                params.append('var_' + var + '=1')
            
            url = self.baseurl + '&'.join(params)
            
            #print 'XPGFS: downloading %s' % (url)
            self.downloading = True
            self.download = AsyncDownload(self.conf, url, cachefile)
            
        return False
    
    def parseGribData(self, filepath, lat, lon):
        '''
        Executes wgrib2 and parses its output
        '''
        args = ['-s',
                '-lon',
                '%f' % (lon),
                '%f' % (lat),
                os.sep.join([self.conf.cachepath, filepath])
                ]
        if self.conf.spinfo:
            p = subprocess.Popen([self.conf.wgrib2bin] + args, stdout=subprocess.PIPE, startupinfo=self.conf.spinfo)
        else:
            p = subprocess.Popen([self.conf.wgrib2bin] + args, stdout=subprocess.PIPE)
        it = iter(p.stdout)
        data = {}
        clouds = {}
        pressure = False
        for line in it:
            r = line[:-1].split(':')
            # Level, variable, value
            level, variable, value = [r[4].split(' '),  r[3],  r[7].split(',')[2].split('=')[1]]
            
            if len(level) > 1:
                if level[1] == 'cloud':
                    #cloud layer
                    clouds.setdefault(level[0], {})
                    if len(level) > 3 and variable == 'PRES':
                        clouds[level[0]][level[2]] = value
                    else:
                        #level coverage/temperature
                        clouds[level[0]][variable] = value
                elif level[1] == 'mb':
                    # wind levels
                    data.setdefault(level[0], {})
                    data[level[0]][variable] = value
                elif level[0] == 'mean':
                    if variable == 'PRMSL':
                        pressure = c.pa2inhg(float(value))
    
        windlevels = []
        cloudlevels = []
        
        # Let data ready to push on datarefs.
        
        # Convert wind levels
        for level in data:
            wind = data[level]
            if 'UGRD' in wind and 'VGRD' in wind:
                hdg, vel = c.c2p(float(wind['UGRD']), float(wind['VGRD']))
                #print wind['UGRD'], wind['VGRD'], float(wind['UGRD']), float(wind['VGRD']), hdg, vel
                alt = c.mb2alt(float(level))
                
                # Optional varialbes
                temp, vis = False, False
                # Temperature
                if 'TMP' in wind:
                    temp = c.oat2msltemp(float(wind['TMP']), alt)
                # Relative Humidity
                #if 'RH' in wind:
                #    vis = c.rh2visibility(float(wind['RH']))*1000
                #    if vis > 40000:
                #        vis = 40000
                else:
                    temp = False
                windlevels.append((alt, hdg, c.ms2knots(vel), {'temp': temp, 'vis': vis}))
                #print 'alt: %i rh: %i vis: %i' % (alt, float(wind['RH']), vis) 
        
        # Convert cloud level
        for level in clouds:
            level = clouds[level]
            if 'top' in level and 'bottom' in level and 'TCDC' in level:
                top, bottom, cover = float(level['top']), float(level['bottom']), float(level['TCDC'])
                #print "XPGFS: top: %.0fmbar %.0fm, bottom: %.0fmbar %.0fm %d%%" % (top * 0.01, c.mb2alt(top * 0.01), bottom * 0.01, c.mb2alt(bottom * 0.01), cover)
                
                cloudlevels.append((c.mb2alt(bottom * 0.01) * 0.3048, c.mb2alt(top * 0.01) * 0.3048, int(c.cc2xp(cover))))
                #XP10 cloudlevels.append((c.mb2alt(bottom * 0.01) * 0.3048, c.mb2alt(top * 0.01) * 0.3048, cover/10))
    
        windlevels.sort()        
        cloudlevels.sort(reverse=True)
        
        #del data
        #del clouds
        
        self.lock.acquire()
        self.winds  = windlevels
        self.clouds = cloudlevels
        self.nwinds = len(windlevels)
        self.nclouds = len(cloudlevels)
        self.parsed_latlon = (lat, lon)
                
        if pressure:
            self.pressure = pressure
        else:
            self.pressure = False
        self.lock.release()
        
        data = {
                'winds': windlevels,
                'clouds': cloudlevels,
                'pressure': pressure
                }
        
        return data 
    
    def reparse(self):
        self.lastlat = False
        self.lastlon = False
        self.newGrib = True
