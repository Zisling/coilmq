"""
Queue storage module that stores the queue information and frames in a DBM-style database.

The current implementation uses the Python `shelve` module, which uses a DBM implementation
under the hood (specifically the `anydbm` module, aka `dbm` in Python 3.x).

Because of how the `shelve` module works (and how we're using it) and caveats in the Python 
documentation this is likely a BAD storage module to use if you are expecting to traffic in
large frames.
"""
__authors__ = ['"Hans Lellelid" <hans@xmpl.org>']
__copyright__ = "Copyright 2009 Hans Lellelid"
__license__ = """Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
 
  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License."""

import threading
import logging
import os
import os.path
import shelve
from collections import deque
from datetime import datetime, timedelta

from coilmq.store import QueueStore
from coilmq.config import config
from coilmq.exception import ConfigError
from coilmq.util.concurrency import synchronized

def make_dbm():
    """
    Creates a DBM queue store, pulling config values from the CoilMQ configuration.
    """
    data_dir = config.get('coilmq', 'store.dbm.data_dir')
    if not data_dir:
        raise ConfigError('Missing configuration parameter: store.dbm.data_dir')
    if not os.path.exists(data_dir):
        raise ConfigError('DBM directory does not exist: %s' % data_dir)
    if not os.access(data_dir, os.W_OK | os.R_OK): # FIXME: how do these get applied? Is OR appropriate?
        raise ConfigError('Cannot read and write DBM directory: %s' % data_dir)
    
    cp_ops = config.get('coilmq', 'store.dbm.checkpoint_operations')
    cp_timeout = config.get('coilmq', 'store.dbm.checkpoint_timeout')
    
    store = DbmQueue(data_dir, checkpoint_operations=cp_ops, checkpoint_timeout=cp_timeout)
    return store

class DbmQueue(QueueStore):
    """
    A QueueStore implementation that stores messages and queue information in DBM-style
    database.
    
    Several database files will be used to support this functionality: metadata about the
    queues will be stored in its own database and each queue will also have its own
    database file.
    
    This classes uses a C{threading.RLock} to guard access to the memory store, since it
    appears that at least some of the underlying implementations that anydbm uses are not
    thread-safe
    
    Due to some impedence mismatch between the types of data we need to store in queues
    (specifically lists) and the types of data that are best stored in DBM databases
    (specifically dicts), this class uses the `shelve` module to abstract away some
    of the ugliness.  The consequence of this is that we only persist objects periodically
    to the datastore, for performance reasons.  How periodic is determined by the 
    `checkpoint_operations` and `checkpoint_timeout` instance variables (and params to 
    L{__init__}).
    
    @ivar data_dir: The directory where DBM files will be stored.
    @type data_dir: C{str}
    
    @ivar queue_metadata: A Shelf (DBM) database that tracks stats & delivered message ids 
                            for all the queues.
    @ivar queue_metadata: C{shelve.Shelf}
    
    @ivar frame_store: A Shelf (DBM) database that contains frame contents indexed by message id.
    @type frame_store: C{shelve.Shelf}
    
    @ivar _opcount: Internal counter for keeping track of unpersisted operations.
    @type _opcount: C{int}
    
    @ivar checkpoint_operations: Number of operations between syncs.
    @type checkpoint_operations: C{int}
    
    @ivar checkpoint_timeout: Max time (in seconds) that can elapse between sync of cache.
    @type checkpoint_timeout: C{float}
    """
    def __init__(self, data_dir, checkpoint_operations=None, checkpoint_timeout=None):
        """
        @param data_dir: The directory where DBM files will be stored.
        @param data_dir: C{str}
        
        @param checkpoint_operations: Number of operations between syncs. (Default = 100)
        @type checkpoint_operations: C{int}
    
        @param checkpoint_timeout: Max time (in seconds) that can elapse between sync of cache. (Default = 20)
        @type checkpoint_timeout: C{float}
        """
        if checkpoint_operations is None: checkpoint_operations = 100  
        if checkpoint_timeout is None: checkpoint_timeout = 20 
        
        self.log = logging.getLogger('%s.%s' % (self.__module__, self.__class__.__name__))
        
        self._lock = threading.RLock()
        self._opcount = 0
        self._last_sync = datetime.now()
        
        self.data_dir = data_dir
        self.checkpoint_operations = checkpoint_operations
        self.checkpoint_timeout = timedelta(seconds=checkpoint_timeout)
        
        # Should this be in constructor?
        self.queue_metadata = shelve.open(os.path.join(self.data_dir, 'metadata'), writeback=True)
        
        # Since we do not need mutable objects on the frame stores (we don't modify them, we just
        # put/get values), we do NOT use writeback=True here.  This should also conserve on memory
        # usage, since apparently that can get hefty with the caching when writeback=True.
        self.frame_store = shelve.open(os.path.join(self.data_dir, 'frames'), writeback=False)
    
    @synchronized
    def enqueue(self, destination, frame):
        """
        Store message (frame) for specified destinationination.
        
        @param destination: The destinationination queue name for this message (frame).
        @type destination: C{str}
        
        @param frame: The message (frame) to send to specified destinationination.
        @type frame: C{coilmq.frame.StompFrame}
        """
        message_id = frame.message_id
        if not message_id:
            raise ValueError("Cannot queue a frame without message-id set.")
        
        if not destination in self.queue_metadata:
            self.log.info("Destination %s not in metadata; creating new entry and queue database." % destination)
            self.queue_metadata[destination] = {'frames': deque(), 'enqueued': 0, 'dequeued': 0, 'size': 0}
            
        self.queue_metadata[destination]['frames'].appendleft(message_id)
        self.queue_metadata[destination]['enqueued'] += 1
        
        self.frame_store[message_id] = frame
        
        self._opcount += 1
        self._sync()
    
    @synchronized
    def dequeue(self, destination):
        """
        Removes and returns an item from the queue (or C{None} if no items in queue).
        
        @param destination: The queue name (destinationination).
        @type destination: C{str}
        
        @return: The first frame in the specified queue, or C{None} if there are none.
        @rtype: L{coilmq.frame.StompFrame} 
        """
        if not self.has_frames(destination):
            return None

        message_id = self.queue_metadata[destination]['frames'].pop()
        self.queue_metadata[destination]['dequeued'] += 1
        
        frame = self.frame_store[message_id]
        del self.frame_store[message_id]
        
        self._opcount += 1
        self._sync()
        
        return frame
    
    @synchronized
    def has_frames(self, destination):
        """
        Whether specified queue has any frames.
        
        @param destination: The queue name (destinationination).
        @type destination: C{str}
        
        @return: Whether there are any frames in the specified queue.
        @rtype: C{bool}
        """
        return (destination in self.queue_metadata) and bool(self.queue_metadata[destination]['frames'])

    @synchronized
    def size(self, destination):
        """
        Size of the queue for specified destination.
        
        @param destination: The queue destination (e.g. /queue/foo)
        @type destination: C{str}
        
        @return: The number of frames in specified queue.
        @rtype: C{int}
        """
        if not destination in self.queue_metadata:
            return 0
        else:
            return len(self.queue_metadata[destination]['frames'])
    
    @synchronized
    def close(self):
        """
        Closes the databases, freeing any resources (and flushing any unsaved changes to disk).
        """
        self.queue_metadata.close()
        self.frame_store.close()
            
    def _sync(self):
        """
        Synchronize the cached data with the underlyind database.
        
        Uses an internal transaction counter and compares to the checkpoint_operations
        and checkpoint_timeout paramters to determine whether to persist the memory store.
        
        In this implementation, this method wraps calls to C{shelve.Shelf#sync}. 
        """
        if (self._opcount > self.checkpoint_operations or 
                datetime.now() > self._last_sync + self.checkpoint_timeout):
            self.log.debug("Synchronizing queue metadata.")
            self.queue_metadata.sync()
            self._last_sync = datetime.now()
            self._opcount = 0
        else:
            self.log.debug("NOT synchronizing queue metadata.")
        