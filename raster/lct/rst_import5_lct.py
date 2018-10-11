import glob, os
from subprocess import Popen, PIPE
import gdal
import numpy as np

# i figure that process outside of postgis then import is better done try to
# do things in gis.  I looked into:
#
# st_mapalgebra see if it can work on band, but that was very slow
#
# st_translate to project.  having hard time to grab correct set of tiles. i
# tied with gist index having shadow geometry in differen projection, but
# maybe because of sinusoidal being funky, i had hard time grabbing
# meaningful number of raster
#
# so basically falling back to the approach of AQRP, but am a bit smater
# (1) merge bands before projection to do things in one shot
# (2) use vrt (virtual raster table) and also -te to window out 10 deg by
# 10 deg projected raster.  it is a pain to create giant raster of entire
# globe especially when resolution is 6sec.  we can merge or mosaic later as
# wish

#year = 2013
knd = 'lct'

# --------------
#   source hdf
# --------------
# hdf files location
ddir = '../../../downloads/e4ftl01.cr.usgs.gov/MOTA/MCD12Q1.006'
# hdf layer names
#lyrnames = ['Land_Cover_Type_1', ]  # for modis c5
lyrnames = ['LC_Type1', ] # for modis c6
# acronym i use, in the order i store data
shortnames = ['lct', ]



# --------------
#   intermediates
# --------------
# workdir
#workdir = './proc_%d' % year
# set of tiff options i always use
tiffopts = ['COMPRESS=LZW','BIGTIFF=YES', 'TILED=YES']

# -----------------
#   destination db
# -----------------
# destination in db
schema = 'raster_6sec'
# tile size in the db
#tilesiz_db=4800
tilesiz_db=240


def get_sdsname( lyrname, fname):
    # given hdf file and layer name, find subdataset name for the layer

    ds = gdal.Open(fname)
    sdss = ds.GetSubDatasets()

    # find subdataset whose name is ending with :lyrname
    sds = [_ for _ in sdss if _[0][-(len(lyrname)+1):] == (':' + lyrname) ]
    try:
        assert len(sds) == 1
    except AssertionError:
        print 'cant find subdataset :%s' % lyrname
        for i,s in enumerate(sdss):
            print i,s
        raise
    sds= sds[0]

    sdsname = sds[0]

    return sdsname


def readclr(fname):
    f = open(fname)
    clr = {}
    for line in f:
        line = [int(_) for _ in line.split()]
        clr[line[0]] = tuple(line[1:])
    return clr


def add_colortable(fname, oname=None, ctval = { 
    (0, 7) : (255, 255, 204, 255),
    (7, 15) : (217, 240, 163, 255),
    (15, 25) : (173, 221, 142, 255),
    (25, 35) : (120, 198, 121, 255),
    (35, 50) : (49, 163, 84, 255),
    (50, 101) : (0, 104, 55, 255),
    249 : (110, 110, 110, 255),
    250 : (255, 0, 0, 255),
    251 : (151, 219, 242, 255),
    252 : (255, 255, 255, 255),
    253 : (115, 0, 0, 255),
    254 : (64, 101, 235, 255),
    255 : (226, 79, 235, 255),
    }):

    """add ctval as color template"""

    if oname is None:
        oname = fname


    drv = gdal.GetDriverByName( 'GTiff' )
    if oname != fname:
        src_ds = gdal.Open(fname, gdal.GA_ReadOnly)
        dst_ds = drv.CreateCopy( oname, src_ds, 0)
    else:
        src_ds = gdal.Open(fname, gdal.GA_Update)
        dst_ds = src_ds

    p = dst_ds.GetRasterBand(1).GetColorTable()

    ct = gdal.ColorTable()
    for k,v in ctval.items():
        if hasattr(k, '__len__'):
            ct.CreateColorRamp(k[0], v, k[1]-1, v)
        else:
            ct.SetColorEntry(k, v)

    dst_ds.GetRasterBand(1).SetColorTable(ct)

    del src_ds, dst_ds
            


def work_import(tifnames, year):
    # create schema if needed
    cmd = 'psql -d finn -c "CREATE SCHEMA IF NOT EXISTS %s;"' %  schema
    os.system(cmd)


    #dstname = schema + '.' + '_'.join([str(_) for _ in (knd, year, 'orig')])
    dstname = schema + '.' + '_'.join([str(_) for _ in (knd, 'global', year)])

    # drop table if it already exists, warn???
    cmd = 'psql -d finn -c "DROP TABLE IF EXISTS %s;"' %  dstname
    os.system(cmd)

    cmd = 'raster2pgsql -d -C -s 4326 -I -M -N 255'.split()
    cmd += ['-t',  '%(tilesiz)sx%(tilesiz)s' % dict(tilesiz=tilesiz_db)]
    cmd += tifnames
    cmd += [dstname]
    print cmd
    fo = open('import_%d.log' % year, 'w')
    p1 = Popen(cmd, stdout=PIPE)
    p2 = Popen(['psql', '-d', 'finn'], stdin=p1.stdout, stdout=fo)
    print p2.communicate()
            

def work_merge(fnames, workdir, dryrun=False):

    # for each file grab three layers.
    # merge into 3band raster
    # then import

    if not os.path.exists(workdir): os.makedirs(workdir)

    buf = []

    for fname in fnames:
        rname = os.path.basename(fname)
        tifname = os.path.join(workdir, rname[:-4] + '.tif')

        sdsnames = [get_sdsname(_, fname) for _ in lyrnames]

        cmd = "gdal_merge.py           -o %(tifname)s -co 'COMPRESS=LZW' -co 'BIGTIFF=YES' -co 'TILED=YES' %(sdsnames)s" % dict(
                tifname=tifname,
                sdsnames = ' '.join(sdsnames))
        #print cmd
        if not dryrun:
            os.system(cmd)

        buf.append(tifname)
    return buf

def work_math(fnames, dstdir):
    if not os.path.exists(dstdir): os.makedirs(dstdir)
    onames = []
    for fname in fnames:
        oname = os.path.join( dstdir, 
                os.path.basename(fname)[:-4] + '.proc.tif')
        print 'proc: %s' % oname
        work_math_one(fname, oname)
        onames.append(oname)
    return onames

def work_math_one(fname, oname):
    # works on 3band raster on tree/herb/bare
    #  pixels with values > 100, convert them into bare unless its nodata

    drv = gdal.GetDriverByName("GTiff")
    ds = gdal.Open(fname, gdal.GA_ReadOnly)
    dso = drv.CreateCopy(oname, ds)

    bands = [ds.GetRasterBand(_) for _ in 1,2,3]
    arr = [_.ReadAsArray() for _ in bands]
    arr[0] = np.where( arr[0] <= 100, arr[0], 0)
    arr[1] = np.where( arr[1] <= 100, arr[1], 0)
    arr[2] = np.where( arr[2] <= 100, arr[2], 100)

    obands = [dso.GetRasterBand(_) for _ in 1,2,3]

    for (b,a) in zip(obands, arr):
        b.WriteArray(a)
    ds = None
    dso = None
    return oname

def work_math_one_inplace(fname):
    # works on 3band raster on tree/herb/bare
    #  pixels with values > 100, convert them into bare unless its nodata
    ds = gdal.Open(fname, gdal.GA_Update)
    bands = [ds.GetRasterBand(_) for _ in 1,2,3]
    arr = [_.ReadAsArray() for _ in bands]
    arr[0][arr[0] > 0 ] = 0
    arr[1][arr[1] > 0 ] = 0
    arr[2][arr[2] > 0 ] = 100

    for (b,a) in zip(bands, arr):
        b.WriteArray(a)
    ds = None

def work_resample_pieces(tifnames, dstdir, bname, dryrun=False):
    # create vrt first, and then generate tiled warped files
    if not os.path.exists(dstdir): os.makedirs(dstdir)
    vrtname = os.path.join(dstdir, 'src.vrt')

    os.system('gdalbuildvrt %s %s'  % ( vrtname, ' '.join(tifnames)))

    res = '-tr 0.00166666666666666666666666666667 0.00166666666666666666666666666667'  # 6 sec
    prj = '-t_srs "+proj=longlat +datum=WGS84 +no_defs"'
    tiffopt = ' '.join(['-co %s' % _ for _ in tiffopts])

    onames = []

    for i in range(36):
        for j in range(18):
            te = '-te %d %d %d %d' % (-180 + 10*i, 90 - 10*(j+1), -180+10*(i+1),
                    90-10*j)
            oname = os.path.join(dstdir, '.'.join([bname, 'h%02dv%02d' % (i,
                j), 'tif']))

            cmd = ( 'gdalwarp %(prj)s %(res)s %(te)s ' + \
                    '-overwrite -r mode -dstnodata 255 ' + \
                    '-wo INIT_DEST=NO_DATA -wo NUM_THREADS=ALL_CPUS ' + \
                    '%(tiffopt)s %(fname)s %(oname)s' ) % dict( 
                        fname=vrtname, oname=oname, 
                        tiffopt=tiffopt, prj=prj, res=res, te=te)
            if not dryrun:

                print te
                ret = os.system(cmd)
                #print ret
            onames.append(oname)
    return onames

def work_vrt(tifnames, oname='xxx.vrt'):
    os.system('gdalbuildvrt %s %s' % (oname, ' '.join(tifnames)))
    return oname

def work_resample_pieces_old(vrtname):
    # hopeing that it runs faster if i resample piece by pieces, provided
    # that i start with vrt
    res = '-tr 0.00166666666666666666666666666667 0.00166666666666666666666666666667'  # 6 sec
    prj = '-t_srs "+proj=longlat +datum=WGS84 +no_defs"'
    te = '-te -100 30 -90 40'
    #te = ''
    tiffopt = ' '.join(['-co %s' % _ for _ in tiffopts])
    cmd = 'gdalwarp %(prj)s %(res)s %(te)s -overwrite  -r average -dstnodata 255 -wo INIT_DEST=NO_DATA -wo NUM_THREADS=ALL_CPUS %(tiffopt)s %(fname)s %(oname)s' % dict(
            fname=vrtname, oname='xxx.tif', tiffopt=tiffopt, prj=prj,
            res=res, te=te)
    print cmd
    os.system(cmd)


def work_resample(tifnames, oname='xxx.tif'):
    #res = '-tr 0.00166666666666666666666666666667 0.00166666666666666666666666666667'  # 6 sec
    #prj = '-t_srs "+proj=longlat +datum=WGS84 +no_defs"'
    #cmd = 'gdalwarp %(prj)s %(res)s -overwrite  -r average -dstnodata 255 -wo INIT_DEST=NO_DATA -wo NUM_THREADS=ALL_CPUS %(tiffopt)s %(fname)s %(oname)s' % dict(
    #        fname=oname2, oname=oname3, tiffopt=tiffopt, prj=prj, res=res)
    res = '0.00166666666666666666666666666667'
    prj = "+proj=longlat +datum=WGS84 +no_defs"
    tiffopts = ['COMPRESS=LZW','BIGTIFF=YES', 'TILED=YES']
    cmd = ['gdalwarp', ]
    cmd += ['-t_srs', prj,]
    cmd += ['-tr' , res, res ]
    cmd += [ '-overwrite', ]
    cmd += [ '-r', 'average' ]
    cmd += [ '-dstnodata', '255' ]
    cmd += [ '-wo', 'INIT_DEST=NO_DATA' ]
    cmd += [ '-wo', 'NUM_THREADS=ALL_CPU' ]
    for opt in tiffopts:
        cmd += [ '-co', opt]
    cmd += tifnames
    cmd += [oname]
    p1 = Popen(cmd)
    p1.communicate()
    return oname


def main(year):
    workdir = './proc_%d' % year

    # grab hdf file names
    fnames = sorted(glob.glob("%(ddir)s/%(year)s.01.01/MCD12Q1.A%(year)s001.h??v??.006.*.hdf" % dict(
            ddir = ddir, year=year)))
    bname = 'MCD12Q1.A%(year)s001' % dict(year=year)
    print 'found %d hdf files:' % len(fnames)


    # merge
    dir_merge = os.path.join(workdir, 'mrg')
    if True:
        # merge bands first as files
        mrgnames = work_merge(fnames, dir_merge)
        #tifnames = work_merge(fnames, dir_merge, dryrun=True)
    else:
        mrgnames = sorted(glob.glob(os.path.join(dir_merge, '*.tif')))

    # resample
    dir_rsmp = os.path.join(workdir, 'rsp')
    if True:
        rsmpnames = work_resample_pieces(mrgnames, dir_rsmp, bname)
        #oname = work_resample(tifnames)
    else:
        rsmpnames = work_resample_pieces(mrgnames, dir_rsmp, bname,
                dryrun=True)


    if True:
        ct = readclr( 'LC_hd_global_2012.tif.clr')
        for fn in rsmpnames:
            print fn
            add_colortable(fn, ctval = ct)
    else:
        pass

    # import
    if True:
        work_import(rsmpnames, year )
        ##work_import(tifnames)
        #work_import([oname])
    else:
        pass

if __name__ == '__main__':
    import sys
    syear = sys.argv[1]
    year = int(syear)

    main(year)
