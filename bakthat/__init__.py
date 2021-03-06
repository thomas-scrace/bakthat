# -*- encoding: utf-8 -*-
import tarfile
import tempfile
import os
import sys
import ConfigParser
from datetime import datetime, timedelta
from getpass import getpass
import logging
import json
import re
import mimetypes
import calendar
from contextlib import closing # for Python2.6 compatibility
import boto
from beefish import decrypt, encrypt_file
import aaargh
import grandfatherson

from bakthat.backends import GlacierBackend, S3Backend, RotationConfig
from bakthat.conf import config, DEFAULT_DESTINATION, DEFAULT_LOCATION

__version__ = "0.3.10"

app = aaargh.App(description="Compress, encrypt and upload files directly to Amazon S3/Glacier.")

log = logging.getLogger(__name__)

if not log.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

STORAGE_BACKEND = dict(s3=S3Backend, glacier=GlacierBackend)

def _get_store_backend(conf, destination=DEFAULT_DESTINATION):
    if not destination:
        destination = config.get("aws", "default_destination")
    return STORAGE_BACKEND[destination](conf)


def _match_filename(filename, destination=DEFAULT_DESTINATION, conf=None):
    """Return all stored backups keys for a given filename."""
    if not filename:
        raise Exception("Filename can't be blank")
    storage_backend = _get_store_backend(conf, destination)

    keys = [name for name in storage_backend.ls() if name.startswith(filename)]
    keys.sort(reverse=True)
    return keys


def match_filename(filename, destination=DEFAULT_DESTINATION, conf=None):
    """Return a list of dict with backup_name, date_component, and is_enc."""
    _keys = _match_filename(filename, destination, conf)
    regex_key = re.compile(r"(?P<backup_name>.+)\.(?P<date_component>\d{14})\.tgz(?P<is_enc>\.enc)?")

    # old regex for backward compatibility (for files without dot before the date component).
    old_regex_key = re.compile(r"(?P<backup_name>.+)(?P<date_component>\d{14})\.tgz(?P<is_enc>\.enc)?")
    
    keys = []
    for key in _keys:
        match = regex_key.match(key)

        # Backward compatibility
        if not match:
            match = old_regex_key.match(key)
        
        if match:
            keys.append(dict(filename=match.group("backup_name"),
                        key=key,
                        backup_date=datetime.strptime(match.group("date_component"), "%Y%m%d%H%M%S"),
                        is_enc=bool(match.group("is_enc"))))
    return keys


def _interval_string_to_seconds(interval_string):
    """Convert internal string like 1M, 1Y3M, 3W to seconds.

    :type interval_string: str
    :param interval_string: Interval string like 1M, 1W, 1M3W4h2s... 
        (s => seconds, m => minutes, h => hours, D => days, W => weeks, M => months, Y => Years).

    :rtype: int
    :return: The conversion in seconds of interval_string.

    """
    interval_exc = "Bad interval format for {0}".format(interval_string)
    interval_dict = {"s": 1, "m": 60, "h": 3600, "D": 86400,
                       "W": 7*86400, "M": 30*86400, "Y": 365*86400}

    interval_regex = re.compile("^(?P<num>[0-9]+)(?P<ext>[smhDWMY])")
    seconds = 0

    while interval_string:
        match = interval_regex.match(interval_string)
        if match:
            num, ext = int(match.group("num")), match.group("ext")
            if num > 0 and ext in interval_dict:
                seconds += num * interval_dict[ext]
                interval_string = interval_string[match.end():]
            else:
                raise Exception(interval_exc)
        else:
            raise Exception(interval_exc)
    return seconds


def _timedelta_total_seconds(td):
    """Python 2.6 backward compatibility function for timedelta.total_seconds.

    :type td: timedelta object
    :param td: timedelta object

    :rtype: float
    :return: The total number of seconds for the given timedelta object.

    """
    if hasattr(timedelta, "total_seconds"):
        return getattr(td, "total_seconds")()

    # Python 2.6 backward compatibility
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / float(10**6)


@app.cmd(help="Delete backups older than the given interval string.")
@app.cmd_arg('-f', '--filename', type=str, default=os.getcwd())
@app.cmd_arg('-i', '--interval', type=str, help="Interval string like 1M, 1W, 1M3W4h2s")
@app.cmd_arg('-d', '--destination', type=str, help="s3|glacier")
def delete_older_than(filename, interval, destination=DEFAULT_DESTINATION, **kwargs):
    """Delete backups matching the given filename older than the given interval string.

    :type filename: str
    :param filename: File/directory name.

    :type interval: str
    :param interval: Interval string like 1M, 1W, 1M3W4h2s... 
        (s => seconds, m => minutes, h => hours, D => days, W => weeks, M => months, Y => Years).

    :type destination: str
    :param destination: glacier|s3

    :type conf: dict
    :keyword conf: Override/set AWS configuration.
        
    :rtype: list
    :return: A list containing the deleted keys (S3) or archives (Glacier).

    """
    conf = kwargs.get("conf")
    storage_backend = _get_store_backend(conf, destination)
    interval_seconds = _interval_string_to_seconds(interval)

    deleted = []

    for key in match_filename(filename, destination, conf):
        backup_age =  _timedelta_total_seconds(datetime.utcnow() - key.get("backup_date"))
        if backup_age > interval_seconds:
            real_key = key.get("key")
            log.info("Deleting {0}".format(real_key))
            storage_backend.delete(real_key)
            deleted.append(real_key)

    return deleted

@app.cmd(help="Rotate backups using Grandfather-father-son backup rotation scheme.")
@app.cmd_arg('-f', '--filename', type=str, default=os.getcwd())
@app.cmd_arg('-d', '--destination', type=str, help="s3|glacier")
def rotate_backups(filename, destination=DEFAULT_DESTINATION, **kwargs):
    """Rotate backup using grandfather-father-son rotation scheme.

    :type filename: str
    :param filename: File/directory name.

    :type destination: str
    :param destination: s3|glacier

    :type conf: dict
    :keyword conf: Override/set AWS configuration.

    :type days: int
    :keyword days: Number of days to keep.

    :type weeks: int
    :keyword weeks: Number of weeks to keep.

    :type months: int
    :keyword months: Number of months to keep.

    :type first_week_day: str
    :keyword first_week_day: First week day (to calculate wich weekly backup keep, saturday by default).
        
    :rtype: list
    :return: A list containing the deleted keys (S3) or archives (Glacier).

    """
    conf = kwargs.get("conf", None)
    storage_backend = _get_store_backend(conf, destination)
    rotate = RotationConfig(kwargs)
    if not rotate:
        raise Exception("You must run bakthat configure_backups_rotation or provide rotation configuration.")

    deleted = []

    backups = match_filename(filename, destination, conf)
    backups_date = [backup["backup_date"] for backup in backups]

    to_delete = grandfatherson.to_delete(backups_date, days=int(rotate.conf["days"]),
                                                    weeks=int(rotate.conf["weeks"]),
                                                    months=int(rotate.conf["months"]),
                                                    firstweekday=int(rotate.conf["first_week_day"]),
                                                    now=datetime.utcnow())
    
    for key in backups:
        if key.get("backup_date") in to_delete:
            real_key = key.get("key")
            log.info("Deleting {0}".format(real_key))
            storage_backend.delete(real_key)
            deleted.append(real_key)

    return deleted

@app.cmd(help="Backup a file or a directory, backup the current directory if no arg is provided.")
@app.cmd_arg('-f', '--filename', type=str, default=os.getcwd())
@app.cmd_arg('-d', '--destination', type=str, help="s3|glacier")
@app.cmd_arg('-p', '--prompt', type=str, help="yes|no", default="yes")
def backup(filename, destination=None, prompt="yes", **kwargs):
    """Perform backup.

    :type filename: str
    :param filename: File/directory to backup.
            
    :type destination: str
    :param destination: s3|glacier

    :type prompt: str
    :param prompt: Disable password promp, disable encryption,
        only useful when using bakthat in command line mode.

    :type password: str
    :keyword password: Password, empty string to disable encryption.

    :type conf: dict
    :keyword conf: Override/set AWS configuration.

    :rtype: dict
    :return: A dict containing the following keys: stored_filename, size, metadata and filename.

    """
    conf = kwargs.get("conf", None)
    storage_backend = _get_store_backend(conf, destination)
    backup_file_fmt = "{0}.{1}.tgz"

    log.info("Backing up " + filename)
    arcname = filename.strip('/').split('/')[-1]
    now = datetime.utcnow()
    date_component = now.strftime("%Y%m%d%H%M%S")
    stored_filename = backup_file_fmt.format(arcname, date_component)
    
    backup_data = dict(filename=arcname, backup_date=int(now.strftime("%s")))

    password = kwargs.get("password")
    if password is None and prompt.lower() != "no":
        password = getpass("Password (blank to disable encryption): ")
        if password:
            password2 = getpass("Password confirmation: ")
            if password != password2:
                log.error("Password confirmation doesn't match")
                return


    # Check if the file is not already compressed
    if mimetypes.guess_type(arcname) == ('application/x-tar', 'gzip'):
        log.info("File already compressed")
        outname = filename

        new_arcname = re.sub(r'(\.t(ar\.)?gz)', '', arcname)
        stored_filename = backup_file_fmt.format(new_arcname, date_component)
        
        with open(outname) as outfile:
            backup_data["size"] = os.fstat(outfile.fileno()).st_size

        bakthat_compression = False
    else:
        # If not we compress it
        log.info("Compressing...")
        with tempfile.NamedTemporaryFile(delete=False) as out:
            with closing(tarfile.open(fileobj=out, mode="w:gz")) as tar:
                tar.add(filename, arcname=arcname)
            outname = out.name
            out.seek(0)
            backup_data["size"] = os.fstat(out.fileno()).st_size
        bakthat_compression = True

    bakthat_encryption = False
    if password:
        bakthat_encryption = True
        log.info("Encrypting...")
        encrypted_out = tempfile.NamedTemporaryFile(delete=False)
        encrypt_file(outname, encrypted_out.name, password)
        stored_filename += ".enc"

        # We only remove the file if the archive is created by bakthat
        if bakthat_compression:
            os.remove(outname)  # remove non-encrypted tmp file

        outname = encrypted_out.name

        encrypted_out.seek(0)
        backup_data["size"] = os.fstat(encrypted_out.fileno()).st_size


    backup_data["metadata"] = dict(is_enc=bakthat_encryption)
    backup_data["stored_filename"] = stored_filename

    log.info("Uploading...")
    storage_backend.upload(stored_filename, outname)

    # We only remove the file if the archive is created by bakthat
    if bakthat_encryption:
        os.remove(outname)

    log.debug(backup_data)
    return backup_data


@app.cmd(help="Give informations about stored filename, current directory if no arg is provided.")
@app.cmd_arg('-f', '--filename', type=str, default=os.getcwd())
@app.cmd_arg('-d', '--destination', type=str, help="s3|glacier")
@app.cmd_arg('-s', '--description', type=str, default=None)
def info(filename, destination=None, description=None, **kwargs):
    conf = kwargs.get("conf", None)
    storage_backend = _get_store_backend(conf, destination)
    filename = filename.split("/")[-1]
    keys = match_filename(filename, destination if destination else DEFAULT_DESTINATION)
    if not keys:
        log.info("No matching backup found for " + str(filename))
        key = None
    else:
        key = keys[0]
        log.info("Last backup date: {0} ({1} versions)".format(key["backup_date"].isoformat(),
                                                    str(len(keys))))
    return key


@app.cmd(help="Set AWS S3/Glacier credentials.")
def configure():
    if not config.has_section("aws"):
        config.add_section("aws")
    
    config.set("aws", "access_key", raw_input("AWS Access Key: "))
    config.set("aws", "secret_key", raw_input("AWS Secret Key: "))
    config.set("aws", "s3_bucket", raw_input("S3 Bucket Name: "))
    config.set("aws", "glacier_vault", raw_input("Glacier Vault Name: "))
    while 1:
        default_destination = raw_input("Default destination ({0}): ".format(DEFAULT_DESTINATION))
        if default_destination:
            default_destination = default_destination.lower()
            if default_destination in ("s3", "glacier"):
                break
            else:
                log.error("Invalid default_destination, should be s3 or glacier, try again.")
        else:
            default_destination = DEFAULT_DESTINATION
            break

    config.set("aws", "default_destination", default_destination)
    region_name = raw_input("Region Name ({0}): ".format(DEFAULT_LOCATION))
    if not region_name:
        region_name = DEFAULT_LOCATION
    config.set("aws", "region_name", region_name)
    config.write(open(os.path.expanduser("~/.bakthat.conf"), "w"))

    log.info("Config written in %s" % os.path.expanduser("~/.bakthat.conf"))
    log.info("Run bakthat configure_backups_rotation if needed.")

@app.cmd(help="Configure backups rotation")
def configure_backups_rotation():
    if not config.has_section("rotation"):
        config.add_section("rotation")
    config.set("rotation", "days", raw_input("Number of days to keep: "))
    config.set("rotation", "weeks", raw_input("Number of weeks to keep: "))
    config.set("rotation", "months", raw_input("Number of months to keep: "))
    while 1:
        first_week_day = raw_input("First week day (to calculate wich weekly backup keep, saturday by default): ")
        if first_week_day:
            if hasattr(calendar, first_week_day.upper()):
                first_week_day = getattr(calendar, first_week_day.upper())
                break
            else:
                log.error("Invalid first_week_day, please choose from sunday to saturday.")
        else:
            first_week_day = calendar.SATURDAY
            break

    config.set("rotation", "first_week_day", str(first_week_day))
    config.write(open(os.path.expanduser("~/.bakthat.conf"), "w"))
    log.info("Config written in %s" % os.path.expanduser("~/.bakthat.conf"))

@app.cmd(help="Restore backup in the current directory.")
@app.cmd_arg('-f', '--filename', type=str, default="")
@app.cmd_arg('-d', '--destination', type=str, help="s3|glacier")
def restore(filename, destination=None, **kwargs):
    """Restore backup in the current working directory.

    :type filename: str
    :param filename: File/directory to backup.
            
    :type destination: str
    :param destination: s3|glacier

    :type conf: dict
    :keyword conf: Override/set AWS configuration.

    :rtype: bool
    :return: True if successful.

    """
    conf = kwargs.get("conf", None)
    storage_backend = _get_store_backend(conf, destination)

    if not filename:
        log.error("No file to restore, use -f to specify one.")
        return

    keys = _match_filename(filename, destination if destination else DEFAULT_DESTINATION)
    if not keys:
        log.error("No file matched.")
        return

    key_name = sorted(keys, reverse=True)[0]
    log.info("Restoring " + key_name)

    # Asking password before actually download to avoid waiting
    if key_name and key_name.endswith(".enc"):
        password = kwargs.get("password")
        if not password:
            password = getpass()

    log.info("Downloading...")
    
    download_kwargs = {}
    if kwargs.get("job_check"):
        download_kwargs["job_check"] = True
        log.info("Job Check: " + repr(download_kwargs))

    out = storage_backend.download(key_name, **download_kwargs)

    if kwargs.get("job_check"):
        log.info("Job Check Request")
        # If it's a job_check call, we return Glacier job data
        return out

    if out and key_name.endswith(".enc"):
        log.info("Decrypting...")
        decrypted_out = tempfile.TemporaryFile()
        decrypt(out, decrypted_out, password)
        out = decrypted_out

    if out:
        log.info("Uncompressing...")
        out.seek(0)
        tar = tarfile.open(fileobj=out)
        tar.extractall()
        tar.close()

        return True


@app.cmd(help="Delete a backup.")
@app.cmd_arg('-f', '--filename', type=str, default="")
@app.cmd_arg('-d', '--destination', type=str, help="s3|glacier")
def delete(filename, destination=None, **kwargs):
    """Delete a backup.

    :type filename: str
    :param filename: File/directory to backup.

    :type destination: str
    :param destination: glacier|s3

    :type conf: dict
    :keyword conf: A dict with a custom configuration.

    :type conf: dict
    :keyword conf: Override/set AWS configuration.

    :rtype: bool
    :return: True if the file is deleted.

    """
    conf = kwargs.get("conf", None)
    storage_backend = _get_store_backend(conf, destination)

    if not filename:
        log.error("No file to delete, use -f to specify one.")
        return

    keys = _match_filename(filename, destination, conf)
    if not keys:
        log.error("No file matched.")
        return

    # Get first matching keys => the most recent
    key_name = keys[0]

    log.info("Deleting " + key_name)

    storage_backend.delete(key_name)

    return True


@app.cmd(help="List stored backups.")
@app.cmd_arg('-d', '--destination', type=str, help="s3|glacier")
def ls(destination=None, **kwargs):
    conf = kwargs.get("conf", None)
    storage_backend = _get_store_backend(conf, destination)
    
    log.info(storage_backend.container)

    ls_result = storage_backend.ls()

    for filename in ls_result:
        log.info(filename)

    return ls_result

@app.cmd(help="Show Glacier inventory from S3")
def show_glacier_inventory(**kwargs):
    if config.get("aws", "s3_bucket"):
        conf = kwargs.get("conf", None)
        glacier_backend = GlacierBackend(conf)
        loaded_archives = glacier_backend.load_archives_from_s3()
        log.info(json.dumps(loaded_archives, sort_keys=True, indent=4, separators=(',', ': ')))
    else:
        log.error("No S3 bucket defined.")
    return loaded_archives


@app.cmd(help="Show local Glacier inventory (from shelve file)")
def show_local_glacier_inventory(**kwargs):
    conf = kwargs.get("conf", None)
    glacier_backend = GlacierBackend(conf)
    archives = glacier_backend.load_archives()
    log.info(json.dumps(archives, sort_keys=True, indent=4, separators=(',', ': ')))
    return archives


@app.cmd(help="Backup Glacier inventory to S3")
def backup_glacier_inventory(**kwargs):
    """Backup Glacier inventory to S3.

    :type conf: dict
    :keyword conf: Override/set AWS configuration.

    """
    conf = kwargs.get("conf", None)
    glacier_backend = GlacierBackend(conf)
    glacier_backend.backup_inventory()


@app.cmd(help="Restore Glacier inventory from S3")
def restore_glacier_inventory(**kwargs):
    """Restore custom Glacier inventory from S3.

    :type conf: dict
    :keyword conf: Override/set AWS configuration.

    """
    conf = kwargs.get("conf", None)
    glacier_backend = GlacierBackend(conf)
    glacier_backend.restore_inventory()


def main():
    app.run()


if __name__ == '__main__':
    main()
