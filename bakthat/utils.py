# -*- encoding: utf-8 -*-
import logging

import bakthat
from bakthat.conf import config, DEFAULT_DESTINATION, DEFAULT_LOCATION

log = logging.getLogger(__name__)


class BakHelper:
    """Helper that makes building scripts with bakthat better faster stronger.

    :type set_name: str
    :param set_name: Backup Set Name
    
    :type destination: str
    :param destination: Destination (glacier|s3)
    
    :type password: str
    :param password: Password (Empty string to disable encryption)

    """
    def __init__(self, set_name, destination=DEFAULT_DESTINATION, password="", **kwargs):
        self.set_name = set_name
        self.destination = destination
        self.password = password
        self.sync = None

    def enable_sync(self, api_url, auth=None):
        """Enable synchronization with BakSyncer (optional).

        :type api_url: str
        :param api_url: Base API URL.
        
        :type auth: tuple
        :param auth: Optional, tuple/list (username, password) for API authentication.

        """
        log.info("Enabling BakSyncer to {0}".format(api_url))
        from bakthat.sync import BakSyncer
        self.sync = BakSyncer(api_url, auth)

    def backup(self, filename, **kwargs):
        """Perform backup.

        :type filename: str
        :param filename: File/directory to backup.
        
        :type password: str
        :keyword password: Override already set password.
        
        :type destination: str
        :keyword destination: Override already set destination.

        :rtype: dict
        :return: A dict containing the following keys: stored_filename, size, metadata and filename.

        """
        password = kwargs.get("password", self.password)
        destination = kwargs.get("destination", self.destination)
        backup = bakthat.backup(filename, destination=destination, password=password)
        
        if self.sync:
            self.sync.post(backup)
        
        return backup

    def restore(self, filename, **kwargs):
        """Restore backup in the current working directory.

        :type filename: str
        :param filename: File/directory to backup.
        
        :type password: str
        :keyword password: Override already set password.
        
        :type destination: str
        :keyword destination: Override already set destination.

        :rtype: bool
        :return: True if successful.

        """
        password = kwargs.get("password", self.password)
        destination = kwargs.get("destination", self.destination)
        return bakthat.restore(filename, destination=destination, password=password)

    def delete_older_than(self, filename, interval, destination):
        """Delete backups older than the given interval string.

        :type filename: str
        :param filename: File/directory name.

        :type interval: str
        :param interval: Interval string like 1M, 1W, 1M3W4h2s... 
            (s => seconds, m => minutes, h => hours, D => days, W => weeks, M => months, Y => Years).

        :type destination: str
        :keyword destination: Override already set destination.
        
        :rtype: list
        :return: A list containing the deleted keys (S3) or archives (Glacier).

        """
        destination = kwargs.get("destination", self.destination)
        deleted = bakthat.delete_older_than(filename, interval, destination=destination)
        
        if self.sync:
            for backup in deleted:
                self.sync.delete(backup)

        return deleted

    def rotate(self, filename, **kwargs):
        """Rotate backup using grandfather-father-son rotation scheme.

        :type filename: str
        :param filename: File/directory name.

        :type destination: str
        :keyword destination: Override already set destination.

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
        log.info("Rotating backup")
        destination = kwargs.pop("destination", self.destination)

        deleted = bakthat.rotate_backups(filename, destination=destination, **kwargs)

        if self.sync:
            for backup in deleted:
                self.sync.delete(backup)

        return deleted