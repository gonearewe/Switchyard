import sys
import argparse
import os
import signal
import re
import subprocess
import time
import threading
import textwrap
from queue import Queue,Empty
from socket import gethostname, if_nametoindex

from .lib.address import *
from .lib.packet import *
from .lib.exceptions import SwitchyException, Shutdown, NoPackets
from .lib.interface import Interface, InterfaceType
from .lib.logging import setup_logging, log_info, log_debug, log_warn, log_failure
from .importcode import import_or_die
from .textcolor import *

from .pcapffi import *
from .llnetbase import LLNetBase, ReceivedPacket

_dlt_to_decoder = {}
_dlt_to_decoder[Dlt.DLT_EN10MB] = lambda raw: Packet(raw, first_header=Ethernet)
_dlt_to_decoder[Dlt.DLT_NULL] = lambda raw: Packet(raw, first_header=Null)

class LLNetReal(LLNetBase):
    '''
    A class that represents a collection of network devices
    on which packets can be received and sent.
    '''
    def __init__(self, devlist, name=None):
        LLNetBase.__init__(self)
        signal.signal(signal.SIGINT, self._sig_handler)
        signal.signal(signal.SIGTERM, self._sig_handler)
        signal.signal(signal.SIGHUP, self._sig_handler)
        signal.signal(signal.SIGUSR1, self._sig_handler)
        signal.signal(signal.SIGUSR2, self._sig_handler)

        self._devs = devlist 
        self._devinfo = self.__assemble_devinfo()
        self._pcaps = {}
        self._pktqueue = None
        self._threads = None
        self.__make_pcaps()
        log_info("Using network devices: {}".format(' '.join(self._devs)))
        for devname, intf in self._devinfo.items():
            log_debug("{}: {}".format(devname, str(intf)))

        LLNetReal.running = True
        self.__spawn_threads()

        if name:
            self.__name = name
        else:
            self.__name = gethostname()

    @property
    def name(self):
        return self.__name

    @property
    def testmode(self):
        return False

    def shutdown(self):
        '''
        Should be called by Switchyard user code when a network object is
        being shut down.  (This method cleans up internal threads and network
        interaction objects.)
        '''
        if not LLNetReal.running:
            return

        LLNetReal.running = False
        log_debug("Joining threads for shutdown")
        for t in self._threads:
            t.join()
        log_debug("Closing pcap devices")
        for devname,pdev in self._pcaps.items():
            pdev.close()
        log_debug("Done cleaning up")

    def __spawn_threads(self):
        '''
        Internal method.  Creates threads to handle low-level
        network receive.
        '''
        self._threads = []
        self._pktqueue = Queue()
        for devname,pdev in self._pcaps.items():
            t = threading.Thread(target=LLNetReal.__low_level_dispatch, args=(pdev, devname, self._pktqueue))
            t.start()
            self._threads.append(t)

    def __assemble_devinfo(self):
        '''
        Internal method.  Assemble information on each interface/
        device that we know about, i.e., its MAC address and configured
        IP address and prefix.
        '''
        devinfo = {}

        # beautiful/ugly regular expressions
        ethaddr_match = '([0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2})'
        hwaddr = re.compile("HWaddr {}".format(ethaddr_match))
        ether = re.compile("ether {}".format(ethaddr_match))

        ipaddr_match = '(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        ipmasklinux = re.compile("inet addr:{0}\s+Bcast:{0}\s+Mask:{0}".format(ipaddr_match))
        ipmaskosx_pat = "inet {}".format(ipaddr_match)
        ipmaskosx_pat += "\s+netmask (0x[0-9a-f]\{8\})"
        ipmaskosx = re.compile(ipmaskosx_pat)

        ip6addr_match = "(?P<ip6addr>[0-9a-f:]+[0-9a-f]{1,4})(%[a-f0-9]+)?"
        ip6maskosx = re.compile("inet6\s+{} netmask (?P<masklen>\d+) scopeid (?P<scope>0x[0-9a-f]+)".format(ip6addr_match))
        ip6masklinux = re.compile("inet6 addr:\s*{}/(?P<masklen>\d+)\s+Scope:(?P<scope>\w+)".format(ip6addr_match))

        devtype = {}
        for p in pcap_devices():
            if p.isloop:
                devtype[p.name] = InterfaceType.Loopback
            else:
                if sys.platform == 'linux':
                    st,output = subprocess.getstatusoutput(["iwconfig", p.name])
                    if "no wireless extensions" in output:
                        devtype[p.name] = InterfaceType.Wired
                    else:
                        devtype[p.name] = InterfaceType.Wireless
                elif sys.platform == 'darwin':
                    devtype[p.name] = InterfaceType.Unknown
                else:
                    devtype[p.name] = InterfaceType.Unknown

        for devname in self._devs:
            macaddr = None
            ipaddr = None
            mask = None
            ip6addr = None
            ip6mask = None
            ip6scope = None
            st,output = subprocess.getstatusoutput(["ifconfig", devname])

            if isinstance(output, bytes):
                output = output.decode('ascii','')
            for line in output.split('\n'):
                mobj = hwaddr.search(line)
                if mobj:
                    macaddr = EthAddr(mobj.groups()[0])
                    continue
                else:
                    mobj = ether.search(line)
                    if mobj:
                        macaddr = EthAddr(mobj.groups()[0])
                        continue
                mobj = ipmasklinux.search(line)
                if mobj:
                    ipaddr = IPAddr(mobj.groups()[0])
                    mask = IPAddr(mobj.groups()[2])
                    continue
                else:
                    mobj = ipmaskosx.search(line)
                    if mobj:
                        ipaddr = IPAddr(mobj.groups()[0])
                        mask = IPAddr(int(mobj.groups()[1], base=16))
                        continue
                mobj = ip6masklinux.search(line)
                if mobj:
                    gd = mobj.groupdict()
                    ip6addr = gd['ip6addr']
                    ip6mask = gd['masklen']
                    ip6scope = gd['scope']
                else:
                    mobj = ip6maskosx.search(line)
                    if mobj:
                        ip6addr = gd['ip6addr']
                        ip6mask = gd['masklen']
                        ip6scope = gd['scope']
                        continue
            ifnum = if_nametoindex(devname)
            devinfo[devname] = Interface(devname, macaddr, ipaddr, netmask=mask, ifnum=ifnum, iftype=devtype[devname])
        return devinfo

    def __make_pcaps(self):
        '''
        Internal method.  Create libpcap devices
        for every network interface we care about and
        set them in non-blocking mode.
        '''
        self._pcaps = {}
        for dev in self._devs:
            thismac = self._devinfo[dev].ethaddr
            pdev = PcapLiveDevice(dev) # default snaplen is 64k
            self._pcaps[dev] = pdev

    def _sig_handler(self, signum, stack):
        '''
        Handle process INT signal.
        '''
        log_debug("Got SIGINT.")
        if signum == signal.SIGINT:
            LLNetReal.running = False
            if self._pktqueue.qsize() == 0:
                # put dummy pkt in queue to unblock a 
                # possibly stuck user thread
                self._pktqueue.put( (None,None,None) )

    @staticmethod
    def __low_level_dispatch(pcapdev, devname, pktqueue):
        '''
        Thread entrypoint for doing low-level receive and dispatch
        for a single pcap device.
        '''
        while LLNetReal.running:
            # a non-zero timeout value is ok here; this is an
            # independent thread that handles input for this
            # one pcap device.  it throws any packets received
            # into the shared queue (which is read by the actual
            # user code)
            pktinfo = pcapdev.recv_packet(timeout=0.2)
            if pktinfo is None:
                continue
            pktqueue.put( (devname,pcapdev.dlt,pktinfo) )

        log_debug("Receiver thread for {} exiting".format(devname))
        stats = pcapdev.stats()
        log_debug("Final device statistics {}: {} received, {} dropped, {} dropped/if".format(devname, stats.ps_recv, stats.ps_drop, stats.ps_ifdrop))

    def recv_packet(self, timeout=None):
        '''
        Receive packets from any device on which one is available.
        Blocks until it receives a packet, unless a timeout value >=0
        is given.  Raises Shutdown exception when device(s) are shut 
        down (i.e., on a SIGINT to the process).  Raises NoPackets when 
        there are no packets that can be read.

        Returns a ReceivedPacket named tuple (timestamp, ingress_dev, packet)
        '''
        while True:
            try:
                dev,dlt,pktinfo = self._pktqueue.get(timeout=timeout)
                if not LLNetReal.running:
                    break

                decoder = _dlt_to_decoder.get(dlt, None)
                if decoder is None:
                    log_warn("Received packet with unparseable encapsulation {}".format(dlt))
                    continue

                pkt = decoder(pktinfo.raw) 
                return ReceivedPacket(timestamp=pktinfo.timestamp, 
                    ingress_dev=dev, packet=pkt)
            except Empty:
                if not LLNetReal.running:
                    raise Shutdown()
                raise NoPackets()
        raise Shutdown()

    def send_packet(self, dev, packet):
        '''
        Send a Switchyard Packet object on the given device 
        (string name of device).

        Raises SwitchyException if packet object isn't valid, or device
        name isn't recognized.
        '''
        if isinstance(dev, int):
           dev = self._lookup_devname(dev)

        if isinstance(dev, Interface):
           dev = dev.name

        pdev = self._pcaps.get(dev, None)
        if not pdev:
            raise SwitchyException("Unrecognized device name for packet send: {}".format(dev))
        else:
            if packet is None:
                raise SwitchyException("No packet object given to send_packet")
            if not isinstance(packet, Packet):
                raise SwitchyException("Object given to send_packet is not a Packet (it is: {})".format(type(packet)))
            # convert packet to bytes and send it
            rawpkt = packet.to_bytes()
            log_debug("Sending packet on device {}: {}".format(dev, str(packet)))
            pdev.send_packet(rawpkt)

def main_real(usercode, netobj, options):
    '''
    Entrypoint function for non-test ("real") mode.  At this point
    we assume that we are running as root and have pcap module.
    '''
    usercode_entry_point = import_or_die(usercode, ('main','srpy_main','switchy_main'))
    if options.dryrun:
        log_info("Imported your code successfully.  Exiting dry run.")
        netobj.shutdown()
        return

    try:
        usercode_entry_point(netobj)
    except Exception as e:
        import traceback

        log_failure("Exception while running your code: {}".format(e))
        message = '''{0}

This is the Switchyard equivalent of the blue screen of death.
Here (repeating what's above) is the failure that occurred:
'''.format('*'*60, textwrap.fill(str(e), 60))
        with red():
            print(message)
            traceback.print_exc(1)
            print('*'*60)

        if options.nohandle:
            raise 
            
        if not options.nopdb:
            print('''
I'm throwing you into the Python debugger (pdb) at the point of failure.
If you don't want pdb, use the --nopdb flag to avoid this fate.
''')
            import pdb
            pdb.post_mortem()
    else:
        netobj.shutdown()
