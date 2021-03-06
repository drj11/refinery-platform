import csv
from datetime import date
import errno
import glob
import logging
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import traceback
import urllib2

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command

from celery.task import task

from core.models import *
from core.utils import index_data_set
from data_set_manager.isa_tab_parser import IsaTabParser
from data_set_manager.models import Investigation, Node, \
    initialize_attribute_order
from data_set_manager.utils import get_node_types, update_annotated_nodes, \
    index_annotated_nodes, calculate_checksum


logger = logging.getLogger(__name__)


def create_dir(file_path):
    """creates a directory if it needs to be created
    Parameters:
    file_path: directory to create if necessary
    """
    try:
        os.makedirs(file_path)
    except OSError, e:
        if e.errno != errno.EEXIST:
            raise


def delete_external_file(file_path):
    """removes a file with the given path (that is outside the file_store)
    Parameters:
    file_path: location of the file to delete
    """
    try:
        os.remove(file_path)
    except OSError:
        raise


@task()
def download_http_file(url, out_dir, accession, new_name=None,
                       galaxy_file_size=None, as_task=True):
    """downloads a file from a given URL
    Parameters:
    url: URL for the file being downloaded
    out_dir: base directory where file is being downloaded
    accession: name of directory that will house the downloaded file, in this
    case, the investigation accession
    """
    # directory where file downloads
    out_dir = os.path.join(out_dir, accession)
    logger.info("data_set_manager.download_http_file called")
    # make super-directory (out_dir/accession) if it doesn't exist
    create_dir(out_dir)
    if new_name is None:
        file_name = url.split('/')[-1]  # get the file name
        # path where file download
        file_path = os.path.join(out_dir, file_name)
    else:
        file_name = new_name
        file_path = os.path.join(out_dir, new_name)

    logger.info("file_path: %s\n" % file_path)
    logger.info("file_name: %s\n" % file_name)
    logger.info("out_dir: %s\n" % out_dir)
    logger.info("url: %s\n" % url)

    if not os.path.exists(file_path):
        u = urllib2.urlopen(url)
        # FIXME: use context manager to open the file
        f = open(file_path, 'wb')
        if galaxy_file_size is None:
            meta = u.info()
            logger.info("meta: %s\n", meta)
            file_size = int(meta.getheaders("Content-Length")[0])
        else:
            file_size = galaxy_file_size

        logger.info("Downloading: %s Bytes: %s", file_name, file_size)
        file_size_dl = 0
        block_sz = 8192
        while True:
            buffer = u.read(block_sz)
            if not buffer:
                break
            file_size_dl += len(buffer)
            f.write(buffer)
            downloaded = file_size_dl * 100. / file_size
            if as_task:
                download_http_file.update_state(
                    state="PROGRESS",
                    meta={
                        "percent_done": "%3.2f%%" % (downloaded),
                        'current': file_size_dl,
                        'total': file_size
                    }
                )
            else:
                status = r"%10d  [%3.2f%%]" % (file_size_dl, downloaded)
                status += chr(8) * (len(status) + 1)
                print status,
        f.close()


def fix_last_col(file):
    """If the header has empty columns in it, then it will delete this and
    corresponding columns in the rows; returns 0 or 1 based on whether it
    failed or was successful, respectively
    Parameters:
    file: name of file to fix
    """
    # TODO: exception handling for file operations (IOError)
    logger.info("trying to fix the last column if necessary")
    # FIXME: use context manager to handle file opening and closing
    reader = csv.reader(open(file, 'rU'), dialect='excel-tab')
    tempfilename = tempfile.NamedTemporaryFile().name
    writer = csv.writer(open(tempfilename, 'wb'), dialect='excel-tab')
    # check that all rows have the same length
    header = reader.next()
    header_length = len(header)
    num_empty_cols = 0  # number of empty header columns
    # TODO: throw exception if there is an empty field in the header between
    # two non-empty fields
    for item in header:
        if not item.strip():
            num_empty_cols += 1
    # write the file
    writer.writerow(header[:-num_empty_cols])
    if num_empty_cols > 0:  # if there are empty header columns
        logger.info("Empty columns in header present, attempting to fix...")
        # check that all the rows are the same length
        line = 0
        for row in reader:
            line += 1
            if len(row) < header_length - num_empty_cols:
                logger.error(
                    "Line " + str(line) + " in the file had fewer fields than "
                    "the header.")
                return False
            # check that all the end columns that are supposed to be empty are
            i = 0
            if len(row) > len(header) - num_empty_cols:
                while i < num_empty_cols:
                    i += 1
                    check_item = row[-i].strip()
                    if check_item:  # item not empty
                        logger.error(
                            "Found a value in " + str(line) +
                            " where an empty column was expected.")
                        return False
                writer.writerow(row[:-num_empty_cols])
            else:
                writer.writerow(row)
        shutil.move(tempfilename, file)
    return True


def zip_converted_files(accession, isatab_zip_loc, preisatab_zip_loc):
    """zips up the isatab and pre-isatab files from MAGE-Tab conversion
    Parameters:
    accession: accession number of investigation
    isatab_zip_loc: prefix for isatab zipped file (dir/accession)
    preisatab_zip_loc: directory where pre-isatab zipped file will be
    """
    logger.info("zipping up ISA-Tab files")
    # send stdout and stderr to a unique temp directory to avoid console
    temp_dir = tempfile.mkdtemp()
    """
    shutil makes a zip, tar, tar.gz, etc file out of files in given dir
    Params:
    zip file prefix
    type of archive (e.g. zip, tar)
    superdirectory of the directory you want to archive
    name of directory that's being archived
    """
    shutil.make_archive(isatab_zip_loc, 'zip', settings.ISA_TAB_DIR, accession)
    # Get and zip up the MAGE-TAB and put in the ISA-Tab folder
    # make file name for ArrayExpress information to download into
    ae_name = tempfile.NamedTemporaryFile(dir=temp_dir, prefix='ae_').name
    # make url to fetch the experiment
    url = "%s/%s" % (settings.AE_BASE_URL, accession)
    # get ArrayExpress information to get URLs to download
    u = urllib2.urlopen(url)
    f = open(ae_name, 'wb')
    f.write(u.read())  # small file, so just grab whole thing in one go
    f.close()
    # open and read in the last line (the HTML) that has the info we want
    f = open(ae_name, 'rb')
    lines = f.readlines()
    f.close()
    last_line = lines[-1]
    dir_to_zip = os.path.join(temp_dir, "magetab")
    create_dir(dir_to_zip)
    # isolate the links by splitting on '<a href="'
    a_hrefs = string.split(last_line, '<a href="')
    # get the links we want
    for a_href in a_hrefs:
        if re.search(r'sdrf.txt', a_href) or re.search(r'idf.txt', a_href):
            link = string.split(a_href, '"').pop(0)  # grab the link
            file_name = link.split('/')[-1]  # get the file name
            if not re.search(r'^http://', link):
                link = "http://www.ebi.ac.uk%s" % link
            u = urllib2.urlopen(link)
            file = os.path.join(dir_to_zip, file_name)
            f = open(file, 'wb')
            f.write(u.read())  # again, shouldn't be a large file
            f.close()
    files_to_zip = 0
    for dirname, dirnames, filenames in os.walk(dir_to_zip):
        for filename in filenames:
            files_to_zip += 1
    if files_to_zip > 1:
        # zip up and move the MAGE-TAB files
        shutil.make_archive("%s/MAGE-TAB_%s" % (preisatab_zip_loc, accession),
                            'zip', temp_dir, "magetab")


@task()
def convert_to_isatab(accession, isatab_zip_loc, preisatab_zip_loc):
    """converts MAGE-Tab file from ArrayExpress into ISA-Tab, zips up the
    ISA-Tab, and zips up the MAGE-Tab
    Parameters:
    accession: ArrayExpress study to convert
    """
    logger = convert_to_isatab.get_logger()
    logger.info("logging from convert_to_isatab")
    # send stdout and stderr to a unique temp directory to avoid console
    temp_dir = tempfile.mkdtemp()
    # create the subprocess
    process = subprocess.Popen(args="./convert.sh", shell=True,
                               cwd=settings.CONVERSION_DIR,
                               stdin=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               stdout=subprocess.PIPE)
    # run the subprocess and grab the exit code
    # exit_code = process.wait()
    logger.info("converting to ISA-Tab")
    (stdout, stderr) = process.communicate(input=accession)
    exit_code = process.returncode
    # process stderr
    if stderr:
        shutil.rmtree(os.path.join(settings.ISA_TAB_DIR, accession))
        logger.error(stderr)
        if exit_code != 0:  # something bad happened
            shutil.rmtree(temp_dir)
            raise Exception("Error Converting to ISA-Tab: %s" % stderr)
        else:
            # unsuccessful conversion, but clean exit
            return "Could not convert %s: %s" % (accession, stderr)
    else:  # successfully converted
        base_dir = os.path.join(settings.ISA_TAB_DIR, accession)
        study_files = glob.glob("%s/s_*.txt" % base_dir)
        assay_files = glob.glob("%s/a_*.txt" % base_dir)
        for study_file in study_files:
            logging.info("fixing last columns in study file if needed")
            if not fix_last_col(study_file):
                return "Could not fix study file for %s" % accession
        for assay_file in assay_files:
            logging.info("fixing last columns in assay file if needed")
            if not fix_last_col(assay_file):
                return "Could not fix assay file for %s" % accession
        zip_converted_files(accession, isatab_zip_loc, preisatab_zip_loc)
    shutil.rmtree(base_dir)
    return "Successfully converted %s" % accession  # successful everything


def get_arrayexpress_studies():
    """task that runs every Friday at 9:00PM that checks ArrayExpress for new
    and updated studies, then pulls down their metadata, converts it to
    ISA-Tab, and parses it into the Django database

    """
    """
    If you don't want to fetch all studies, edit in this fashion:
        call_command('mage2isa_convert', 'exptype=chip-seq', species='human')
    """
    call_command('mage2isa_convert', 'exptype=ChIP-seq')


@task()
def create_dataset(investigation_uuid, username, identifier=None, title=None,
                   dataset_name=None, slug=None, public=False):
    """creates (or updates) a dataset with the given investigation and user and
    returns the dataset UUID or None if something went wrong
    Parameters:
    investigation_uuid: UUID of the investigation that's being assigned to the
    dataset
    username: username of the user this dataset will belong to
    identifier: If not None, this will be used as the identifier of the data
    set.
    title: If not None, this will be
    public: boolean value that determines if the dataset is public or not
    """
    # get User for assigning DataSets
    try:
        user = User.objects.get(username__exact=username)
    except:
        logger.info(
            "create_dataset: User %s doesn't exist, so creating User %s with "
            "password 'test'", username, username)
        # user doesn't exist
        user = User.objects.create_user(username, "", "test")
    if investigation_uuid is not None:
        # TODO: make sure this is used everywhere
        annotate_nodes(investigation_uuid)
        dataset = None
        investigation = Investigation.objects.get(uuid=investigation_uuid)
        if identifier is None:
            identifier = investigation.get_identifier()
        if title is None:
            title = investigation.get_title()
        if dataset_name is None:
            dataset_name = "%s: %s" % (identifier, title)

        logger.info(
            "create_dataset: title = %s, identifer = %s, dataset_name = '%s'",
            title, identifier, dataset_name)

        datasets = DataSet.objects.filter(name=dataset_name)
        # check if the investigation already exists
        if len(datasets):  # if not 0, update dataset with new investigation
            """go through datasets until you find one with the correct owner"""
            for ds in datasets:
                own = ds.get_owner()
                if own == user:
                    ds.update_investigation(investigation,
                                            "updated %s" % date.today())
                    logger.info("create_dataset: Updated data set %s", ds.name)
                    dataset = ds
                    break
        # create a new dataset if doesn't exist already for this user
        if not dataset:
            dataset = DataSet.objects.create(name=dataset_name)
            dataset.set_investigation(investigation)
            dataset.set_owner(user)
            logger.info("create_dataset: Created data set '%s'", dataset_name)
        if public:
            public_group = ExtendedGroup.objects.public_group()
            dataset.share(public_group)
        # set dataset slug
        dataset.slug = slug
        # calculate total number of files and total number of bytes
        dataset.file_size = dataset.get_file_size()
        dataset.file_count = dataset.get_file_count()
        dataset.save()
        # Finally index data set
        index_data_set(dataset)
        return dataset.uuid
    return None


@task()
def annotate_nodes(investigation_uuid):
    """Adds all nodes in this investigation to the annotated nodes table for
    faster lookup
    """
    investigation = Investigation.objects.get(uuid=investigation_uuid)
    studies = investigation.study_set.all()
    for study in studies:
        assays = study.assay_set.all()
        for assay in assays:
            node_types = get_node_types(
                study.uuid, assay.uuid,
                files_only=True,
                filter_set=Node.FILES
            )
            for node_type in node_types:
                update_annotated_nodes(
                    node_type,
                    study.uuid,
                    assay.uuid,
                    update=True
                )
                index_annotated_nodes(node_type, study.uuid, assay.uuid)
            # initialize attribute order for this assay
            attribute_count = initialize_attribute_order(study, assay)


@task()
def parse_isatab(username, public, path,
                 additional_raw_data_file_extension=None, isa_archive=None,
                 pre_isa_archive=None, file_base_path=None):
    """parses in an ISA-TAB file to create database entries and creates or
    updates a dataset for the investigation to belong to; returns the dataset
    UUID or None if something went wrong. Use like this: parse_isatab(username,
    is_public, folder_name, additional_raw_data_file_extension,
    isa_archive=<path>, pre_isa_archive=<path>, file_base_path=<path>
    Parameters:
    username: username of the person the dataset will belong to
    public: boolean that determines if the dataset is public or not
    path: absolute path of the ISA-Tab file to parse
    additional_raw_data_file_extension: an optional argument that will append a
    suffix to items in Raw Data File as need be
    isa_archive: if you're passing a directory, a zipped version of the
    directory for storage and legacy purposes
    pre_isa_archive: optional copy of files that were converted to ISA-Tab
    file_base_path: if your file locations are relative paths, this is the base
    """
    p = IsaTabParser()
    p.additional_raw_data_file_extension = additional_raw_data_file_extension
    p.file_base_path = file_base_path
    """Get the study title and investigation id and see if anything is in the
    database and if so compare the checksum
    """
    # 1. First check whether the user exists
    try:
        user = User.objects.get(username__exact=username)
    except:
        user = None
    # 2. If user exists we need to quickly get the dataset title to see if its
    # already in the DB
    if user:
        checksum = None
        (identifier, title) = p.get_dataset_name(path)
        if identifier is None or title is None:
            datasets = []
        else:
            dataset_title = "%s: %s" % (identifier, title)
            datasets = DataSet.objects.filter(name=dataset_title)
        # check if the investigation already exists
        if len(datasets):  # if not 0, update dataset with new investigation
            # go through datasets until you find one with the correct owner
            for ds in datasets:
                own = ds.get_owner()
                if own == user:
                    # 3. Finally we need to get the checksum so that we can
                    # compare that to our given file.
                    investigation = ds.get_investigation()
                    fileStoreItem = FileStoreItem.objects.get(
                        uuid=investigation.isarchive_file)
                    if fileStoreItem:
                        try:
                            logger.info("Get file: %s",
                                        fileStoreItem.get_absolute_path())
                            checksum = calculate_checksum(
                                fileStoreItem.get_file_object())
                        except IOError as e:
                            logger.error(
                                "Original isatab archive wasn't found. "
                                "Error: '%s'", e)
        # 4. Finally if we got a checksum for an existing file, we calculate
        # the checksum for the new file and compare them
        if checksum:
            new_checksum = None
            # TODO: error handling
            with open(path, 'rb') as f:
                new_checksum = calculate_checksum(f)
            if (checksum == new_checksum):
                # Checksums are identical so we can skip this file.
                logger.info("The checksum of both files is the same: %s",
                            checksum)
                return (
                    investigation.investigationlink_set.all()[0].data_set.uuid,
                    os.path.basename(path),
                    True)
    try:
        investigation = p.run(path, isa_archive=isa_archive,
                              preisa_archive=pre_isa_archive)
        data_uuid = create_dataset(investigation.uuid, username, public=public)
        return (data_uuid, os.path.basename(path), False)
    except:  # prints the error message without breaking things
        logger.error("*** print_tb:")
        exc_type, exc_value, exc_traceback = sys.exc_info()
        logger.error(traceback.print_tb(exc_traceback, file=sys.stdout))
        logger.error("*** print_exception:")
        logger.error(
            traceback.print_exception(
                exc_type,
                exc_value,
                exc_traceback,
                file=sys.stdout
            )
        )
    return None, os.path.basename(path), False
