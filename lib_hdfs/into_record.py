"""Read satellite image data from geotif or hdf files and write patches into tfrecords.
Parallelized with mpi4py.
"""
__author__  = "casperneo@uchicago.edu"
__author2__ = "tkurihana@uchicago.edu"

import tensorflow as tf
import os
import cv2
import json
import glob
import copy
import numpy as np
#import seaborn as sns

from osgeo import gdal
from mpi4py import MPI
#from matplotlib import pyplot as plt
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
from pyhdf.SD import SD, SDC


def _int64_feature(value):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=value))


def _bytes_feature(value):
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def _float_feature(value):
    return tf.train.Feature(float_list=tf.train.FloatList(value=value))


def gen_swaths(fnames, mode, resize):
    """Reads and yields resized swaths.
    Args:
        fnames: Iterable of filenames to read
        mode: {"mod09_tif", "mod02_1km"} determines wheter to processes the file as a tif
            or a hdf file
        resize: Float or None - factor to resize the image by e.g. 0.5 to halve height and
            width. If resize is none then no resizing is performed.
    Yields:
        filename, (resized) swath
    """

    # Define helper function to catch the exception from gdal directly
    def gdOpen(file):
        # print('Filename being opened by gdal:',file, flush=True)
        try:
            output = gdal.Open(file).ReadAsArray()
        except IOError:
            print("Error while opening file:", file, flush=True)
        return output

    if mode == "mod09_tif":
        # read = lambda tif_file: gdal.Open(tif_file).ReadAsArray()
        read = lambda tif_file: gdOpen(tif_file)

    elif mode == "mod02_1km":
        names_1km = {
            "EV_250_Aggr1km_RefSB": [0, 1],
            "EV_500_Aggr1km_RefSB": [0, 1],
            "EV_1KM_RefSB": [x for x in range(15) if x not in (12, 14)],
            # 6,7 are very noisy water vapor channels
            "EV_1KM_Emissive": [0, 1, 2, 3, 10, 11],
        }
        read = lambda hdf_file: read_hdf(hdf_file, names_1km)

    else:
        raise ValueError("Invalid reader mode", mode)

    rank = MPI.COMM_WORLD.Get_rank()
    for t in fnames:
        print("rank", rank, "reading", t, flush=True)

        try:
            swath = np.rollaxis(read(t), 0, 3)
        except Exception as e:
            print(rank, "Could not read", t, "because", e)
            continue

        if resize is not None:
            swath = cv2.resize(
                swath, dsize=None, fx=resize, fy=resize, interpolation=cv2.INTER_AREA
            )
        yield t, swath


def read_hdf(hdf_file, fields, x_range=(None, None), y_range=(None, None)):
    """Read `hdf_file` and extract relevant fields as per `names_1km`.
    """
    x_min, x_max = x_range
    y_min, y_max = y_range
    hdf = SD(hdf_file, SDC.READ)

    swath = [hdf.select(f)[:, x_min:x_max, y_min:y_max][fields[f]] for f in fields]
    return np.concatenate(swath, axis=0)


def gen_patches(swaths, shape, strides):
    """Normalizes swaths and yields patches of size `shape` every `strides` pixels
    Args:
        swaths: Iterable of (filename, np.ndarray) to slice patches from
        shape: (height, width) patch size
        strides: (x_steps, y_steps) how many pixels between patches
    Yields:
        (filename, coordinate, patch): where the coordinate is the pixel coordinate of the
        patch inside of filename. BUG: pixel coorindate is miscalculated if swath is
        resized. Patches come from the swath in random order and are whiten-normalized.
    """
    stride_x, stride_y = strides
    shape_x, shape_y = shape

    for fname, swath in swaths:
        # NOTE: Normalizing the whole (sometimes 8gb) swath will double memory usage
        # by casting it from int16 to float32. Instead normalize and cast patches.
        # TODO other kinds of normalization e.g. max scaling.
        mean = swath.mean(axis=(0, 1)).astype(np.float32)
        std = swath.std(axis=(0, 1)).astype(np.float32)
        max_x, max_y, _ = swath.shape

        # Shuffle patches
        coords = []
        for x in range(0, max_x, stride_x):
            for y in range(0, max_y, stride_y):
                if x + shape_x < max_x and y + shape_y < max_y:
                    coords.append((x, y))
        np.random.shuffle(coords)

        for x, y in coords:
            patch = swath[x : x + shape_x, y : y + shape_y]
            # Filter away patches with Nans or if every channel is over 50% 1 value
            # Ie low cloud fraction.
            threshold = shape_x * shape_y * 0.5
            max_uniq = lambda c: max(np.unique(patch[:, :, c], return_counts=True)[1])
            has_clouds = any(max_uniq(c) < threshold for c in range(patch.shape[-1]))
            if has_clouds:
                patch = (patch.astype(np.float32) - mean) / std
                if not np.isnan(patch).any():
                    yield fname, (x, y), patch


def write_feature(writer, filename, coord, patch):
    feature = {
        "filename": _bytes_feature(bytes(filename, encoding="utf-8")),
        "coordinate": _int64_feature(coord),
        "shape": _int64_feature(patch.shape),
        "patch": _bytes_feature(patch.ravel().tobytes()),
    }
    example = tf.train.Example(features=tf.train.Features(feature=feature))
    writer.write(example.SerializeToString())

def old_get_blob_ratio(patch):
    """ + Document  
    ** This scheme may not be suitable. 2018/12/24
        Compute Ratio of white parts in images
    """
    img = copy.deepcopy(patch[:,:,0])
    nmax = np.amax(img)
    nmin = np.amin(img)
    # normalization
    img += abs(nmin)  # range 0 -  max+abs(min)
    img  = img / (nmax + abs(nmin)) * 255  # range 0 -255   
    blob_ratio = len(np.where(img > 35 )[0])/(img.shape[0]**2)*100
    # bone ==> higher number gets whiter color 
    #_img = np.rint(img) TODO: conv float to int and cut .0000
    # check for color
    #sns.heatmap(img[:11,:11].astype(dtype='int32'), 
    #            annot=True, fmt="d", cmap='bone', vmin=0, vmax=255)
    return blob_ratio


def get_blob_ratio(patch, thres_val=0.00):
    """ Compute Ratio of non-negative pixels in an image
        thres_val : threshold vale; defualt is 0/non-negative value
    """
    img = copy.deepcopy(patch[:,:,0]).flatten()
    clouds_ratio = len(np.argwhere(img > thres_val))/len(img)*100
    return clouds_ratio
    

def interactive_writer(patches, categories, out_dir="", isHistOn=True):
    """Writes patches to categories interactively.
    """
    writers = [
        tf.python_io.TFRecordWriter(os.path.join(out_dir, c + ".tfrecord"))
        for c in categories
    ]
    categories += ["Noise"]

    # Add distribution check
    if isHistOn:
        def _loading(filename):
            return np.load(filename)

        try:
            numpy_datadir = "/home/tkurihana/scratch-midway2/clouds"
            if "close" in categories or "closed" in categories:
                dist_array = _loading(numpy_datadir+'/closed_array.npy')
                clabel='closed'
            elif "open" in categories:
                dist_array   = _loading(numpy_datadir+'/open_array.npy')
                clabel='open'
            dweights = np.ones(len(dist_array[:,:,:,0].flatten()))/float(len(dist_array[:,:,:,0].flatten()))
        except:
            raise NameError(" File not found: Check file directory again")


    for filename, coord, patch in patches:
        # check while/black ration
        clouds_ratio = get_blob_ratio(patch)
        print( " ### Clouds Ratio == %f ###  " % clouds_ratio)
        if isHistOn:
            plt.figure()
            plt.hist(dist_array[:,:,:,0].flatten(), density=True, weights=dweights, alpha=0.3, label=clabel)
            weights = np.ones(len(patch[:,:,0].flatten()))/float(len(patch[:,:,0].flatten()))
            plt.hist(patch[:,:,0].flatten(), density=True, weights=weights, alpha=0.3, label='Patch')
            plt.legend(fontsize=18)
            plt.show()
        plt.figure()
        plt.imshow(patch[:, :, 0], cmap="bone")
        plt.show(block=False)
        #plt.show()
        while True:
            print("Please label the patch:")
            for i, cat in enumerate(categories):
                print("({}): {}".format(i, cat))

            try:
                label = int(input("label:"))
                assert 0 <= label <= len(writers), "label not in range"

                if label < len(writers):
                    write_feature(writers[label], filename, coord, patch)
                else:
                    print("Patch thrown away as noise")

                break

            except KeyboardInterrupt as e:
                print("Keyboard interrupt detected, exiting.")
                exit()

            except Exception as e:
                print("Exception caught:", e)
        plt.close()

    print("All patches processed. Thank you!")


def write_patches(patches, out_dir, patches_per_record):
    """Writes `patches_per_record` patches into a tfrecord file in `out_dir`.
    Args:
        patches: Iterable of (filename, coordinate, patch) which defines tfrecord example
            to write.
        out_dir: Directory to save tfrecords.
        patches_per_record: Number of examples to save in each tfrecord.
    Side Effect:
        Examples are written to `out_dir`. File format is `out_dir`/`rank`-`k`.tfrecord
        where k means its the "k^th" record that `rank` has written.
    """
    rank = MPI.COMM_WORLD.Get_rank()
    for i, patch in enumerate(patches):
        if i % patches_per_record == 0:
            rec = "{}-{}.tfrecord".format(rank, i // patches_per_record)
            print("Writing to", rec, flush=True)
            f = tf.python_io.TFRecordWriter(os.path.join(out_dir, rec))

        write_feature(f, *patch)

        print("Rank", rank, "wrote", i + 1, "patches", flush=True)


def get_args(verbose=False):
    p = ArgumentParser(
        formatter_class=ArgumentDefaultsHelpFormatter, description=__doc__
    )
    p.add_argument("source_glob", help="Glob of files to convert to tfrecord")
    p.add_argument("out_dir", help="Directory to save results")
    p.add_argument(
        "mode",
        choices=["mod09_tif", "mod02_1km"],
        help="`mod09_tif`: Turn whole .tif swath into tfrecord. "
        "`mod02_1km` : Extracts EV_250_Aggr1km_RefSB, EV_500_Aggr1km_RefSB, "
        "EV_1KM_RefSB, and EV_1KM_Emissive.",
    )
    p.add_argument(
        "--shape",
        nargs=2,
        type=int,
        help="patch shape. Only used for pptif",
        default=(128, 128),
    )
    p.add_argument(
        "--resize",
        type=float,
        help="Resize fraction e.g. 0.25 to quarter scale. Only used for pptif",
    )
    p.add_argument(
        "--stride",
        nargs=2,
        type=int,
        help="patch stride. Only used for pptif",
        default=(64, 64),
    )
    p.add_argument(
        "--patches_per_record", type=int, help="Only used for pptif", default=500
    )
    p.add_argument(
        "--interactive_categories",
        nargs="+",
        metavar="c",
        help="Categories for manually labeling patches. 'Noise' category will be added "
        "and those patches thrown away automatically.",
    )

    FLAGS = p.parse_args()
    if verbose:
        for f in FLAGS.__dict__:
            print("\t", f, (25 - len(f)) * " ", FLAGS.__dict__[f])
        print("\n")

    FLAGS.out_dir = os.path.abspath(FLAGS.out_dir)
    return FLAGS


if __name__ == "__main__":
    comm = MPI.COMM_WORLD
    size = comm.Get_size()
    rank = comm.Get_rank()

    FLAGS = get_args(verbose=rank == 0)
    os.makedirs(FLAGS.out_dir, exist_ok=True)

    fnames = []
    for i, f in enumerate(sorted(glob.glob(FLAGS.source_glob))):
        if i % size == rank:
            fnames.append(os.path.abspath(f))

    if not fnames:
        raise ValueError("source_glob does not match any files")

    swaths = gen_swaths(fnames, FLAGS.mode, FLAGS.resize)
    patches = gen_patches(swaths, FLAGS.shape, FLAGS.stride)

    if FLAGS.interactive_categories is None:
        write_patches(patches, FLAGS.out_dir, FLAGS.patches_per_record)

    else:
        interactive_writer(patches, FLAGS.interactive_categories, FLAGS.out_dir)

    print("Rank %d done." % rank, flush=True)
