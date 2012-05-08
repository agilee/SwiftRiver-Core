#!/usr/bin/env python
# encoding: utf-8
"""
Extracts links from the droplets posted to the metadata fanout exchange
and publishes the update droplet back to the DROPLET_QUEUE for updating
in the db

Copyright (c) 2012 Ushahidi. All rights reserved.
"""

import sys
import ConfigParser
import logging as log
import pika
import json
import re
from threading import Event
from httplib2 import Http
from os.path import dirname, realpath
from swiftriver import Daemon, Worker


class LinkExtractorQueueWorker(Worker):

    def __init__(self, name, mq_host, queue, options=None):
        Worker.__init__(self, name, mq_host, queue, options)
        self.start()

    def handle_mq_response(self, ch, method, properties, body):
        """POSTs the droplet to the semantics API"""
        droplet = None
        try:
            droplet = json.loads(body)
        except ValueError, e:
            # Bad value in the queue, skip it
            log.error(" %s bad value received in the queue" % (self.name,))
            ch.basic_ack(delivery_tag method.delivery_tag)
            return

        log.info(" %s droplet received with id %d" %
            (self.name, droplet.get('id', 0)))

        # Strip tags leaving only hyperlinks
        droplet_raw = re.sub(r'<(?!\s*[aA]\s*)[^>]*?>', '',
                             droplet['droplet_raw'].strip())

        droplet_raw = droplet_raw.encode('ascii', 'ignore')

        # Extract the href from hyperlinks since the regex above expects
        # a space character at the end of the url
        droplet_raw = re.sub(
            r'(?i)<(?=\s*[a]\s+)[^>]*href\s*=\s*"([^"]*)"[^>]*?>', ' \\1 ',
            droplet_raw)

        for link in re.findall("(?:https?://[^\\s]+)", droplet_raw):
            if not 'links' in droplet:
                droplet['links'] = []

            if link[:4] != 'http':
                link = 'http://' + link

            m = re.search('https?://([^/]+)', link)
            domain = ''
            if m:
                domain = m.group(1)

            # Get the full URL but only do so if the link
            # looks like a shortened url
            if len(link) < 25 and len(domain) < 10:
                log.debug(" %s expanding url %s" % (self.name, link))
                h = Http()
                try:
                    resp, content = h.request(link, 'HEAD')
                    link = resp.get('content-location', link)
                except Exception, e:
                    log.error(" %s error expanding url %r" % (self.name, e))

            droplet['links'].append(link)

        # Send back the updated droplet to the droplet queue for updating
        droplet['links_complete'] = True
        droplet_channel = self.mq.channel()
        droplet_channel.queue_declare(queue=self.DROPLET_QUEUE, durable=True)

        droplet_channel.basic_publish(
            exchange='',
            routing_key=self.DROPLET_QUEUE,
            properties=pika.BasicProperties(
                delivery_mode=2, # make message persistent
            ),
            body=json.dumps(droplet))

        droplet_channel.close()

        # Confirm delivery only once droplet has been passed
        # for metadata extraction
        ch.basic_ack(delivery_tag=method.delivery_tag)
        log.info(" %s finished processing" % (self.name,))


class LinkExtractorQueueDaemon(Daemon):

    def __init__(self, num_workers, mq_host, pid_file, out_file):
        Daemon.__init__(self, pid_file, out_file, out_file, out_file)

        self.num_workers = num_workers
        self.mq_host = mq_host

    def run(self):
        event = Event()

        queue_name = 'LINK_EXTRACTOR_QUEUE'
        options = {
            'exchange_name': 'metadata',
            'exchange_type': 'fanout',
            'durable_queue': True}

        for x in range(self.num_workers):
            LinkExtractorQueueWorker("linkextractor-worker-" + str(x),
                                     self.mq_host, queue_name, options)

        log.info("Workers started")
        event.wait()
        log.info("Exiting")


if __name__ == "__main__":
    config = ConfigParser.SafeConfigParser()
    config.readfp(open(dirname(
        realpath(__file__)) + '/config/linkextractor.cfg'))

    try:
        log_file = config.get("main", 'log_file')
        out_file = config.get("main", 'out_file')
        pid_file = config.get("main", 'pid_file')
        num_workers = config.getint("main", 'num_workers')
        log_level = config.get("main", 'log_level')
        mq_host = config.get("main", 'mq_host')

        FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        log.basicConfig(filename=log_file,
                        level=getattr(log, log_level.upper()),
                        format=FORMAT)

        # Create outfile if it does not exist
        file(out_file, 'a')

        # Create reference for the daemon
        daemon = LinkExtractorQueueDaemon(num_workers, mq_host,
                                          pid_file, out_file)
        if len(sys.argv) == 2:
            if 'start' == sys.argv[1]:
                daemon.start()
            elif 'stop' == sys.argv[1]:
                daemon.stop()
            elif 'restart' == sys.argv[1]:
                daemon.restart()
            else:
                print "Unknown command"
                sys.exit(2)
            sys.exit(0)
        else:
            print "usage: %s start|stop|restart" % sys.argv[0]
            sys.exit(2)
    except ConfigParser.NoOptionError, e:
        log.error(" Configuration error:  %s" % e)