#!/usr/bin/env python
# -*- coding: utf-8 -*-

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

from __future__ import print_function # for python 2

__author__ = "David Aikema, <david.aikema@uct.ac.za>"

import json
import pika
import random
import string
import uuid

from pika import exceptions
from pika.adapters import twisted_connection
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.logger import Logger
from twisted.python.failure import Failure
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET

class SubmitException (Exception):
  def __init__(self, msg, job_id=None):
    self.msg = msg
    self.job_id = job_id
  def toJSON (self):
    result = { 'error': True, 'job_id': self.job_id, 'msg': self.msg }
    return json.dumps(result) + "\n"
    

# API function fall: submitTransfer
# (submit a transfer)
class TransferSubmit (Resource):
  isLeaf = True
  def __init__(self, dbpool, staging_queue, conn):
    Resource.__init__(self)

    # Use global logger and database pool
    self.log = Logger()
    self.dbpool = dbpool

    # Setup local connection to rabbitmq
    self.staging_queue = staging_queue
    self.pika_conn = conn
    self.pika_send_properties = pika.BasicProperties(content_type='text/plain',
                                                     delivery_mode=1)

  def render_POST(self, request):
  	# Check fields and return an error if any are missing
    for formvar in ['productID', 'destinationPath']:
      if formvar not in request.args:
        request.setResponseCode(400)
        result = {
          'msg': 'Form did not specify {0}'.format(formvar),
          'job_id': None,
          'error': True,
        }
        return json.dumps(result) + "\n"

    job_uuid = str(uuid.uuid1())
    callback = ''.join(random.choice(string.lowercase) for i in range(32))

    # Create database record
    # (with status set to 'ERROR' until the job is added to rabbitmq)
    def add_initial(txn):
      self.log.debug("Running add_initial")
      try:
        txn.execute("INSERT INTO jobs (jobid, productid, status, destination_path, "
                    "stager_callback, time_submitted) VALUES (%s, %s, 'ERROR', %s, "
                    "%s, now())", [job_uuid, request.args['productID'][0],
                    request.args['destinationPath'][0], callback])
      except Exception, e:
        self.log.error(e)
        request.setResponseCode(500)
        raise SubmitException('Error creating database record', job_uuid)
      self.log.debug("Done running add_initial {0}".format(job_uuid))

    # Add to rabbitmq
    @inlineCallbacks
    def add_to_rabbitmq(_):
      self.log.debug("Running add_to_rabbitmq")
      channel = yield self.pika_conn.channel()
      yield channel.queue_declare(queue=self.staging_queue,
                                  exclusive=False, durable=False)
      yield channel.basic_publish('', self.staging_queue, job_uuid,
                                  self.pika_send_properties)

    def update_job_status_submitted(txn):
      self.log.debug("Running update_job_status_submitted")
      pass

      # Update database record state to STAGING
      try:
        txn.execute("UPDATE jobs SET status = 'STAGING' WHERE jobid = %s", [job_uuid])
      except Exception, e:
        self.log.error(e)
        request.setResponseCode(500)
        msg = 'Error updating job status to STAGING'
        result = {
          'msg': msg,
          'job_id': job_uuid,
          'error': True
        }
        return json.dumps(result) + "\n"

    # Report results
    def report_job_creation(_):
      self.log.debug("Running report_job_creation")
      result = {
        'msg': 'Job submission processed succesfully',
        'error': False,
        'job_id': job_uuid
      }
      request.setResponseCode(202)
      request.write(json.dumps(result) + "\n")
      request.finish()

    def handleCreationError(e):
      request.setResponseCode(500)
      if isinstance(SubmitException, e.value):
        request.write(e.toJSON())
      else:
        self.log.error(e)
        result = {
          'msg': 'Unknown error handling job submission',
          'job_id': job_uuid,
          'error': True
        }
        request.write(json.dumps(result) + "\n")
      request.finish()
      #return(e)
        
    d = self.dbpool.runInteraction(add_initial)
    d.addCallback(add_to_rabbitmq)
    d.addCallback(lambda _: self.dbpool.runInteraction(update_job_status_submitted))
    d.addCallback(report_job_creation)
    d.addErrback(handleCreationError)

    return NOT_DONE_YET
