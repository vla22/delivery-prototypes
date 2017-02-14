#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Manage interaction with FTS service."""
# Copyright 2017  University of Cape Town
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function  # for python 2

import fts3.rest.client.easy as fts3
import json
import os
import pika
import twisted

from os.path import basename
from sys import stderr
from time import sleep
from twisted.internet import reactor
from twisted.internet.defer import DeferredSemaphore, inlineCallbacks, \
                                   returnValue
from twisted.internet.task import LoopingCall
from twisted.logger import Logger

__author__ = "David Aikema, <david.aikema@uct.ac.za>"


# FTS Updater
# (scans FTS server at a regular interval, updating the status of tasks)
@inlineCallbacks
def _FTSUpdater():
    """Contact FTS to update the status of jobs in the TRANSFERRING state."""
    global _log
    global _dbpool
    global _fts_params
    global _sem_fts

    _log.info("Running FTS updater")

    # Initialize FTS context
    try:
        fts_context = fts3.Context(*_fts_params)
    except Exception, e:
        _log.error('Exception creating FTS context in _FTSUpdater')
        _log.error(str(e))
        returnValue(None)

    # Retrieve list of jobs currently submitted to FTS from DB
    try:
        r = yield _dbpool.runQuery("SELECT job_id, fts_jobid FROM jobs WHERE "
                                   "status='TRANSFERRING'")
    except Exception, e:
        _log.error('Error retrieving list of in transferring stage from DB')
        _log.error(str(e))
        returnValue(None)

    if r is None:
        _log.info('FTS Updater found no jobs in the transferring state')
        returnValue(None)

    # for each job get FTS status
    try:
        for job in r:
            yield _log.debug('job_id: %s | fts_id: %s' % (job[0], job[1]))
            job_id = job[0]
            fts_jobid = job[1]
            fts_job_status = fts3.get_job_status(fts_context, fts_jobid)

            # Compare and update
            state = fts_job_status['job_state']

            if state == 'FINISHED':
                _log.info('Job %s successfully completed using FTS' % job_id)
                yield _dbpool.runQuery("UPDATE jobs SET status = 'SUCCESS', "
                                       "fts_details = %s WHERE job_id = %s",
                                       [str(fts_job_status), job_id])
                yield _sem_fts.release()
            elif state == 'FAILED':
                _log.info('Job %s has failed during the transfer stage'
                          % job_id)
                yield _dbpool.runQuery("UPDATE jobs SET status = 'ERROR', "
                                       "fts_details = %s WHERE job_id = %s",
                                       [str(fts_job_status), job_id])
                yield _sem_fts.release()
            else:
                yield _dbpool.runQuery("UPDATE jobs SET fts_details = %s "
                                       "WHERE job_id = %s",
                                       [str(fts_job_status), job_id])
    except Exception, e:
        _log.error('Error updating status for jobs in FTS manager')
        _log.error(str(e))
        returnValue(None)
    _log.info('FTS Updater finished updating jobs that were in transferring '
              'state')


@inlineCallbacks
def _start_fts_transfer(job_id):
    """Submit transfer request for job to FTS server and update DB."""
    global _log
    global _dbpool
    global _fts_params

    try:
        fts_context = fts3.Context(*_fts_params)
    except Exception, e:
        _log.error('Exception creating FTS context in _start_fts_transfer')
        _log.error(str(e))
        ds = "Failed to create FTS context when setting up transfer"
        _dbpool.runQuery("UPDATE jobs SET status='ERROR', extra_status = %s"
                         " WHERE job_id = %s", [ds, job_id])
        returnValue(None)

    # Get information about the transfer from the database
    r = yield _dbpool.runQuery("SELECT stager_path, stager_hostname, "
                               "destination_path FROM jobs WHERE job_id = %s",
                               [job_id])
    if r is None:
        _log.error('Invalid job_id %s received by FTS transfer service'
                   % job_id)
        returnValue(None)

    # Create the transfer request
    src = 'gsiftp://%s%s' % (r[0][1], str(r[0][0]).rstrip(os.sep))
    dst = '%s/%s' % (str(r[0][2]).rstrip('/'), basename(r[0][0]))
    _log.info("About to transfer '%s' to '%s' for job %s" %
              (src, dst, job_id))
    try:
        transfer = fts3.new_transfer(src, dst)
        fts_job = fts3.new_job([transfer])
        fts_jobid = fts3.submit(fts_context, fts_job)
        fts_job_status = fts3.get_job_status(fts_context, fts_jobid)
    except Exception, e:
        _log.error('Error submitting job %s to FTS' % job_id)
        _log.error(str(e))
        ds = "Error submitting job to FTS"
        _dbpool.runQuery("UPDATE jobs SET status='ERROR', extra_status = "
                         "%s WHERE job_id = %s", [ds, job_id])
        returnValue(None)

    # Update job status, add FTS job ID & FTS status
    try:
        yield _dbpool.runQuery("UPDATE jobs SET status='TRANSFERRING', "
                               "fts_jobid = %s, "
                               "fts_details = %s WHERE job_id = %s",
                               [fts_jobid, str(fts_job_status), job_id])
    except Exception, e:
        _log.error('Error updating status for job %s' % job_id)
        _log.error(str(e))
        ds = "Error updating job status following FTS submission"
        _dbpool.runQuery("UPDATE jobs SET status='ERROR', extra_status "
                         "= %s WHERE job_id = %s", [ds, job_id])

        returnValue(None)
    _log.info('Job database updated; add FTS ID %s for job %s'
              % (fts_jobid, job_id))


@inlineCallbacks
def _transfer_queue_listener():
    """Wait for requests to come in via transfer queue.

    Note that only a bounded number of jobs are permitted
    to be in the transferring state at any point in time and this is enforced
    using a semaphore.
    """
    global _log
    global _transfer_queue
    global _pika_conn
    global _sem_fts

    channel = yield _pika_conn.channel()

    queue = yield channel.queue_declare(queue=_transfer_queue,
                                        exclusive=False,
                                        durable=True)
    yield channel.basic_qos(prefetch_count=1)

    # Enter loop
    queue, consumer_tag = yield channel.basic_consume(queue=_transfer_queue,
                                                      no_ack=False)

    while True:
        ch, method, properties, body = yield queue.get()
        if body:
            yield _sem_fts.acquire()
            reactor.callFromThread(_start_fts_transfer, body)
            yield ch.basic_ack(delivery_tag=method.delivery_tag)


def init_fts_manager(pika_conn, dbpool, fts_params, transfer_queue,
                     concurrent_max, polling_interval):
    """Initialize services to manage transfers using FTS.

    This involves:
    * Initializing a thread to listen for requests to start
      transfers on the transfer queue
    * Scheduling a routine to run at a regular interval, querying the
      FTS server to update the status of jobs in the TRANSFERRING state.

    Note that this function also initializes a semaphore used to enforce a
    limit on the maximum number of transfer tasks which are permitted to take
    place in parallel.

    Parameters:
    pika_conn -- Global shared connection for RabbitMQ
    dbpool -- Global shared database connection pool
    fts_params -- A list of parameters to initialize the FTS service
        [URI of FTS server, path to certificate, path to key]
    transfer_queue -- Name of the RabbitMQ queue to which to listen for
      transfer requests
    concurrent_max -- Maximum number of jobs that are permitted to be
      in the TRANSFERRING stage at any point in time
    polling_interval -- Interval in seconds between polling attempts of the
      FTS server to update the status of jobs currently in the TRANSFERRING
      state
    """
    global _log
    global _dbpool
    global _fts_params
    global _pika_conn
    global _transfer_queue
    global _sem_fts

    _log = Logger()

    _pika_conn = pika_conn
    _dbpool = dbpool
    _fts_params = fts_params
    _transfer_queue = transfer_queue
    _sem_fts = DeferredSemaphore(int(concurrent_max))

    # Start queue listener
    reactor.callFromThread(_transfer_queue_listener)

    # Run a task at a regular interval to update the status of submitted jobs
    fts_updater_runner = LoopingCall(_FTSUpdater)
    fts_updater_runner.start(int(polling_interval))
