#!/usr/bin/python
# -*- coding: latin-1 -*-
# -----------------------------------------------------------------------------
# Copyright 2010-2012 Stephen Tiedemann <stephen.tiedemann@googlemail.com>
#
# Licensed under the EUPL, Version 1.1 or - as soon they 
# will be approved by the European Commission - subsequent
# versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the
# Licence.
# You may obtain a copy of the Licence at:
#
# http://www.osor.eu/eupl
#
# Unless required by applicable law or agreed to in
# writing, software distributed under the Licence is
# distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied.
# See the Licence for the specific language governing
# permissions and limitations under the Licence.
# -----------------------------------------------------------------------------

import logging
log = logging.getLogger()

import os
import sys
import time
import string
import struct
import os.path
import inspect
import threading
import Queue as queue

sys.path.insert(1, os.path.split(sys.path[0])[0])
import nfc
import nfc.ndef

def trace(func):
    def traced_func(*args, **kwargs):
        _args = "{0}".format(args[1:]).strip("(),")
        if kwargs:
            _args = ', '.join([_args, "{0}".format(kwargs).strip("{}")])
        log.debug("{func}({args})".format(func=func.__name__, args=_args))
        return func(*args, **kwargs)
    return traced_func

def printable(data):
    printable = string.digits + string.letters + string.punctuation + ' '
    return ''.join([c if c in printable else '.' for c in data])

def format_data(data):
    s = []
    for i in range(0, len(data), 16):
        s.append("  %04x: " % i)
        s[-1] += ' '.join(["%02x" % ord(c) for c in data[i:i+16]]) + ' '
        s[-1] += (8 + 16*3 - len(s[-1])) * ' '
        s[-1] += printable(data[i:i+16])
    return '\n'.join(s)

class PhdcManager(threading.Thread):
    def __init__(self):
        super(PhdcManager, self).__init__()
        self.oqueue = queue.Queue()
        self.iqueue = queue.Queue()

    def enqueue(self, apdu):
        if apdu is None or len(apdu) > 0:
            self.iqueue.put(apdu)

    def dequeue(self):
        try:
            apdu = self.oqueue.get(block=True, timeout=0.1)
        except queue.Empty:
            apdu = ""
        return apdu
                
    def send(self, apdu):
        self.oqueue.put(apdu)

    def recv(self, timeout):
        try:
            return self.iqueue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

class PhdcTagManager(PhdcManager):
    def __init__(self, tag, apdu):
        super(PhdcTagManager, self).__init__()
        self.enqueue(apdu)
        self.tag = tag
        self.mc = 1

    @trace
    def read_phd_message(self, timeout):
        t0 = time.time()
        while True:
            time.sleep(0.01)
            try:
                message = nfc.ndef.Message(self.tag.ndef.message)
            except nfc.ndef.LengthError:
                if int((time.time() - t0) * 1000) > timeout:
                    return None
                continue
            if message.type == "urn:nfc:wkt:PHD":
                data = bytearray(message[0].data)
                if data[0] & 0x0F == (self.mc % 4) << 2 | 2:
                    log.info("[phdc] <<< {0}".format(str(data).encode("hex")))
                    if isinstance(self.tag, nfc.Type3Tag):
                        attr = nfc.tt3.NdefAttributeData(self.tag.read([0]))
                        attr.writing = True; attr.length = 0
                        self.tag.write(str(attr), [0])
                    self.mc += 1
                    return data[1:]

    @trace
    def write_phd_message(self, apdu):
        data = bytearray([(self.mc % 4) << 2 | 3]) + apdu
        record = nfc.ndef.Record("urn:nfc:wkt:PHD", data=str(data))
        log.info("[phdc] >>> {0}".format(record.data.encode("hex")))
        self.tag.ndef.message = str(nfc.ndef.Message(record))
        self.mc += 1
        
    def run(self):
        log.info("entering phdc manager run loop")
        while True:
            try:
                apdu = self.dequeue()
                self.write_phd_message(apdu)
                apdu = self.read_phd_message(timeout=100)
                self.enqueue(apdu)
            except IOError:
                self.enqueue(None)
                break
        log.info("leaving phdc manager run loop")
        

def phdc_tag_manager(args):
    tag = poll(args.clf)
    if tag is None:
        raise SystemExit(1)

    print(tag)
    if tag.ndef:
        print("NDEF attribute data:")
        print("  version   = %s" % tag.ndef.version)
        print("  writeable = %s" % ("no", "yes")[tag.ndef.writeable])
        print("  capacity  = %d byte" % tag.ndef.capacity)
        print("  data size = %d byte" % len(tag.ndef.message))
        if len(tag.ndef.message):
            print("NDEF message dump:")
            print(format_data(tag.ndef.message))
            message = nfc.ndef.Message(tag.ndef.message)
            print(message.pretty())

    if tag.ndef:
        message = nfc.ndef.Message(tag.ndef.message)
        if message.type == "urn:nfc:wkt:PHD":
            phd_data = bytearray(message[0].data)
            if phd_data[0] == 0:
                manager = PhdcTagManager(tag, apdu=phd_data[1:])
                manager.start()
                log.info("entering ieee manager echo loop")
                while True:
                    apdu = manager.recv(timeout=None)
                    if apdu is None: break
                    log.info("[ieee] <<< {0}".format(str(apdu).encode("hex")))
                    apdu = apdu[::-1]
                    time.sleep(0.2)
                    log.info("[ieee] >>> {0}".format(str(apdu).encode("hex")))
                    manager.send(apdu)
                log.info("leaving ieee manager echo loop")
    
        
def poll(clf):
    try:
        while True:
            tag = clf.poll()
            if tag: return tag
            else: time.sleep(0.5)
    except KeyboardInterrupt:
        return None

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-q", dest="quiet", action="store_true",
        help="do not print any log messages'")
    parser.add_argument(
        "-d", metavar="MODULE", dest="debug", action="append",
        help="print debug messages for MODULE, use '' for all")
    parser.add_argument(
        "-f", dest="logfile", metavar="FILE",
        help="write log messages to file")
    parser.add_argument(
        "-l", "--loop", action='store_true',
        help="repeat command until Control-C")
    parser.add_argument(
        "--no-wait", action='store_true',
        help="do not wait for tag removal")
    parser.add_argument(
        "--device", metavar="NAME", action="append",
        help="use specified contactless reader(s): "\
            "usb[:vendor[:product]] (vendor and product in hex), "\
            "usb[:bus[:dev]] (bus and device number in decimal), "\
            "tty[:(usb|com)[:port]] (usb virtual or com port)")

    options = parser.parse_args()

    logformat = '%(message)s'
    verbosity = logging.ERROR if options.quiet else logging.INFO
        
    if options.debug:
        logformat = '%(levelname)-5s [%(name)s] %(message)s'
        if '' in options.debug:
            verbosity = logging.DEBUG
        
    logging.basicConfig(level=verbosity, format=logformat)

    if options.debug and 'nfc' in options.debug:
        verbosity = logging.DEBUG
            
    if options.logfile:
        logfile_format = \
            '%(asctime)s %(levelname)-5s [%(name)s] %(message)s'
        logfile = logging.FileHandler(options.logfile, "w")
        logfile.setFormatter(logging.Formatter(logfile_format))
        logfile.setLevel(logging.DEBUG)
        logging.getLogger('').addHandler(logfile)

    nfcpy_path = os.path.dirname(inspect.getfile(nfc))
    for name in os.listdir(nfcpy_path):
        if os.path.isdir(os.path.join(nfcpy_path, name)):
            logging.getLogger("nfc."+name).setLevel(verbosity)
        elif name.endswith(".py") and name != "__init__.py":
            logging.getLogger("nfc."+name[:-3]).setLevel(verbosity)

    if options.debug:
        for module in options.debug:
            log.info("enable debug output for module '{0}'".format(module))
            logging.getLogger(module).setLevel(logging.DEBUG)

    if options.device is None:
        options.device = ['']
            
    for device in options.device:
        try:
            options.clf = nfc.ContactlessFrontend(device);
            break
        except LookupError:
            pass
    else:
        log.warning("no contactless reader")
        raise SystemExit(1)

    try:
        while True:
            log.info("waiting for agent")
            phdc_tag_manager(options)
            if not options.loop:
                break
    except KeyboardInterrupt:
        raise SystemExit
    finally:
        options.clf.close()
    
