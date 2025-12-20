#!/usr/bin/env python3

"""
(c) 2025 Rainer Fiedler - do6uk
https://github.com/do6uk/meshcore_multitcp

"""

import threading
import socket
import os
import sys
import signal
import argparse
import traceback
import json
import time
import sqlite3
from typing import Any, Dict
from meshcore_multitcp_packets import BinaryReqType, PacketType, CmdPacketType

APP = "MeshCore multitcp-proxy"
VERSION = "1.1"

import logging
FORMAT = '%(asctime)-15s %(levelname)-10s %(message)s'
logging.basicConfig(format=FORMAT, level=logging.INFO)
logger = logging.getLogger(__name__)

client_host = "0.0.0.0"
client_port = 5000

forward_host = "0.0.0.0"
forward_port = 5001

device_host = "192.168.5.62"
device_port = 5000

sqlite_active = False
sqlite_file = 'meshcore_multitcp.db'

clients = []
client_names = {}
threads = []
client_fwd = {}
client_fwd_active = {}

stop_device = False
device = None

#ignore_packets_device = [PacketType.BATTERY.value, PacketType.ADVERTISEMENT.value]
#ignore_packets_device = [PacketType.NO_MORE_MSGS.value, PacketType.OK.value, PacketType.BATTERY.value, PacketType.DEVICE_INFO.value, PacketType.LOG_DATA.value]
ignore_packets_device = [PacketType.OK.value, PacketType.BATTERY.value, PacketType.DEVICE_INFO.value, PacketType.LOG_DATA.value]

#ignore_packets_app = [CmdPacketType.GET_BATT_AND_STORAGE.value]
ignore_packets_app = []

app_frame_started = False
app_frame_size = 0
app_header = b""
app_inframe = b""

device_frame_started = False
device_frame_size = 0
device_header = b""
device_inframe = b""

contact_nb = 0
contacts = {}
channels = {}

def db_create():
	global sqlite_file
	
	logger.info(f"SQLITE: creating {sqlite_file} ...")
	con = sqlite3.connect(sqlite_file)
	cur = con.cursor()
	cur.execute('CREATE TABLE packets (timestamp INTEGER, type INTEGER, data BLOB)')
	cur.execute('CREATE TABLE clients (hash TEXT, ip TEXT, client TEXT, last_timestamp INTEGER)')
	con.commit()
	con.close()

def db_store_packet(packet_timestamp, packet_type, data):
	global sqlite_file
	
	logger.debug(f"SQLITE: storing message to {sqlite_file} ...")
	con = sqlite3.connect(sqlite_file)
	cur = con.cursor()
	cur.execute(f"INSERT INTO packets (timestamp, type, data) VALUES ({packet_timestamp}, {packet_type}, ?)", (data,))
	con.commit()
	con.close()

def db_load_packet(ip,clientname):
	global sqlite_file, client_fwd
	
	if client_fwd[ip]:
		logger.debug(f"SQLITE: {ip} ({clientname}) messages already synced")
		return False
	
	logger.info(f"SQLITE: restoring data from {sqlite_file} for {clientname}@{ip}...")
	
	con = sqlite3.connect(sqlite_file)
	con.text_factory = bytes
	cur = con.cursor()
	res = cur.execute(f"SELECT last_timestamp FROM clients WHERE ip = ? AND client = ?",(ip,clientname))
	row = res.fetchone()
	if row:
		timestamp = row[0]
	else:
		cur.execute(f"INSERT INTO clients (hash, ip, client, last_timestamp) VALUES ('',?,?,0)",(ip,clientname))
		con.commit()
		timestamp = 0
	
	logger.debug(f"SQLITE: {ip} ({clientname}) requests stored messages after {timestamp}")
	res = cur.execute(f"SELECT data FROM packets WHERE timestamp > {timestamp}")
	rows = res.fetchall()
	
	data = []
	for row in rows:
		data.append(row[0])
	
	con.close()
	return data

def db_set_client_time(ip,clientname):
	global sqlite_file
	
	logger.debug(f"SQLITE: set timestamp for {clientname}@{ip} ...")
	
	con = sqlite3.connect(sqlite_file)
	cur = con.cursor()
	cur.execute(f"UPDATE clients SET last_timestamp = ? WHERE client = ? AND ip = ?", (time.time(),clientname,ip))
	con.commit()
	con.close()

def get_ip(sock_handle):
	try:
		host, port = sock_handle.getpeername()
		return host
	except:
		logger.error(f"[get_ip] while getpeername()")
		return False

def get_ip_port(sock_handle):
	try:
		host, port = sock_handle.getpeername()
		return host+':'+str(port)
	except:
		logger.error(f"[get_ip_port] while getpeername()")
		return False

def get_ip_client_name(sock_handle):
	global client_names
	try:
		host, port = sock_handle.getpeername()
	except:
		logger.error(f"[get_ip_client_name] while getpeername()")
		return False
	
	try:
		client_name = host+' ('+client_names[host+':'+str(port)]+')'
	except:
		client_name = host+':'+str(port)
	
	return client_name

def get_client_name(sock_handle):
	global client_names
	try:
		host, port = sock_handle.getpeername()
	except:
		logger.error(f"[get_client_name] while getpeername()")
		return False
	
	try:
		client_name = client_names[host+':'+str(port)]
	except:
		client_name = host+':'+str(port)
	
	return client_name

def ip_to_tuple(ip):
	ip, port = ip.split(':')
	return (ip, int(port))
	
def handle_app_data(data: bytearray):
	if len(data) >= 3:
		frame_header = data[:3]
		frame_size = int.from_bytes(frame_header[1:], byteorder="little")
		frame_data = data[3:]
		logger.debug(f"[handle_app_data] header: {frame_header} size: {frame_size} data: {frame_data}")
		
		packet_type_value = data[3]
		
		if packet_type_value in ignore_packets_app:
			logger.debug(f"[handle_app_data] packet {CmdPacketType(packet_type_value).name} ignored")
			return False
		
		if CmdPacketType.exists(packet_type_value):
			packet_type = CmdPacketType(packet_type_value).name
			logger.debug(f"[handle_app_data] packet {packet_type} found ...")
			return packet_type
	
	elif len(data) == 0:
		logger.debug(f"[handle_app_data] connection lost")
		return -1
	
	else:
		logger.error(f"[handle_app_data] header_len_error | data: {data}")
		return False

def handle_device_data(data: bytearray):
	if len(data) >= 3:
		frame_header = data[:3]
		frame_size = int.from_bytes(frame_header[1:], byteorder="little")
		frame_data = data[3:]
		logger.debug(f"[handle_device_data] header: {frame_header} size: {frame_size} data: {frame_data}")
		
		packet_type_value = data[3]
		logger.debug(f"[parse_device_data] raw-data: {data.hex()}")
		
		if packet_type_value in ignore_packets_device:
			logger.debug(f"[parse_device_data] packet {PacketType(packet_type_value).name} ignored")
			return False
		
		if PacketType.exists(packet_type_value):
			packet_type = PacketType(packet_type_value).name
			logger.debug(f"[parse_device_data] packet {packet_type} found ...")
			
			if packet_type_value in [7,8,16,17] and sqlite_active:
				db_store_packet(time.time(),packet_type_value,data)
			
			return packet_type
	
	elif len(data) == 0:
		logger.debug(f"[handle_device_data] connection lost")
		return -1
	else:
		logger.error(f"[handle_device_data] header_len_error | data: {data}")
		return False



def parse_status(data, pubkey_prefix=None, offset=0):
	res = {}
	
	# Handle pubkey
	if pubkey_prefix is None:
		# Extract from data (format 1)
		res["pubkey_pre"] = data[2:8].hex()
		offset = 8  # Fields start at offset 8
	else:
		# Use provided prefix (format 2)
		res["pubkey_pre"] = pubkey_prefix
		# offset stays as provided (typically 0)
	
	# Parse all fields with the given offset
	res["bat"] = int.from_bytes(data[offset:offset+2], byteorder="little")
	res["tx_queue_len"] = int.from_bytes(data[offset+2:offset+4], byteorder="little")
	res["noise_floor"] = int.from_bytes(data[offset+4:offset+6], byteorder="little", signed=True)
	res["last_rssi"] = int.from_bytes(data[offset+6:offset+8], byteorder="little", signed=True)
	res["nb_recv"] = int.from_bytes(data[offset+8:offset+12], byteorder="little", signed=False)
	res["nb_sent"] = int.from_bytes(data[offset+12:offset+16], byteorder="little", signed=False)
	res["airtime"] = int.from_bytes(data[offset+16:offset+20], byteorder="little")
	res["uptime"] = int.from_bytes(data[offset+20:offset+24], byteorder="little")
	res["sent_flood"] = int.from_bytes(data[offset+24:offset+28], byteorder="little")
	res["sent_direct"] = int.from_bytes(data[offset+28:offset+32], byteorder="little")
	res["recv_flood"] = int.from_bytes(data[offset+32:offset+36], byteorder="little")
	res["recv_direct"] = int.from_bytes(data[offset+36:offset+40], byteorder="little")
	res["full_evts"] = int.from_bytes(data[offset+40:offset+42], byteorder="little")
	res["last_snr"] = int.from_bytes(data[offset+42:offset+44], byteorder="little", signed=True) / 4
	res["direct_dups"] = int.from_bytes(data[offset+44:offset+46], byteorder="little")
	res["flood_dups"] = int.from_bytes(data[offset+46:offset+48], byteorder="little")
	res["rx_airtime"] = int.from_bytes(data[offset+48:offset+52], byteorder="little")
	
	return res

def device_connect():
	global device
	
	try:
		device = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		device.connect((device_host, device_port))
		return True
	
	except:
		return False

def device_handle():
	global stop_device, threads, device_host, device_port
	
	count_retry = 0

	while not stop_device:
		logger.debug(f"DEVICE: connect to {device_host}:{device_port} attemp {count_retry+1} ...")
		
		count_retry = count_retry + 1
		device_ready = device_connect()
		if device_ready:
			logger.info(f"DEVICE: ready ...")
			count_retry = 0
		
		while device_ready:
			try:
				message = device.recv(1024)
				logger.debug(f"DEVICE: receive raw: {message}")
				message_type = handle_device_data(message)
				
				if message_type == -1:
					raise socket.error
				
				if message_type and message_type != 'NONE':
					logger.info(f"DEVICE {message_type}")
				
				client_forward(message, message_type)
				
				if stop_device:
					return
			
			except socket.error:
				logger.error(f"DEVICE: lost connection on read {get_ip(device)}")
				device.close()
				break
	
		if count_retry == 3:
			stop_device = True
			logger.error(f"DEVICE: re-connect failed {count_retry} times ... exit")
			os.kill(os.getpid(), signal.SIGINT)
			
		logger.debug(f"DEVICE: re-connect failed {count_retry} times ... try again")
		time.sleep(2*count_retry)

def device_write(message):
	global device, stop_device
	
	if not stop_device:
		try:
			logger.debug(f"DEVICE: send raw: {message}")
			device.send(message)
			return True
		
		except (socket.error, BrokenPipeError):
			logger.error(f"DEVICE: lost connection on write {get_ip(device)}")
			device.close()
			return False
	
	else:
		logger.error(f"DEVICE: closed ... exit")
		sys.exit(1)

def client_forward(message, message_type):
	global client_fwd, client_fwd_active
	
	for client in clients:
		ip = get_ip(client)
		ipclientname = get_ip_client_name(client)
		if message_type == -1:
			logger.debug(f"CLIENT {ipclientname} lost connection on Forward")
			raise socket.error
		elif message_type and message_type != 'NONE':
			logger.debug(f"CLIENT {ipclientname} Forward {message_type}")
		else:
			logger.debug(f"CLIENT {ipclientname} Forward raw-data: {message}")
			
		try:
			count_wait = 0
			while client_fwd_active[ip]:
				count_wait += 1
				logger.debug(f"CLIENT {ipclientname} Forward is waiting while syncing stored messages ({count_wait}/3) ...")
				
				if count_wait == 3:
					client_fwd[ip] = True
					client_fwd_active[ip] = False
					logger.info(f"CLIENT {ipclientname} possible crash of message-sync! forward anyway ...")
				
				time.sleep(5)
			
			client.send(message)
		
		except (socket.error, BrokenPipeError):
			logger.error(f"CLIENT {get_ip_client_name(client)} lost connection on write")
			if client in clients:
				index = clients.index(client)
			clients.remove(client)
			client.close()
			continue

def client_receive(client, forwarding = False):
	global client_fwd, client_fwd_active, stop_device
	
	while not stop_device:
		try:
			ip = get_ip(client)
			ipclientname = get_ip_client_name(client)
			
			message = client.recv(1024)
			message_type = handle_app_data(message)
			
			if message_type == -1:
				logger.debug(f"CLIENT {ipclientname} lost connection on send")
				raise socket.error
			elif message_type:
				logger.info(f"CLIENT {ipclientname} Sent {message_type}")
			else:
				logger.debug(f"CLIENT {ipclientname} Sent raw-data: {message}")
			
			if message_type == CmdPacketType.APP_START.name:
				app_ver = message[4]
				app_name = message[11:].decode('utf-8')
				client_names[get_ip_port(client)] = app_name
				logger.debug(f"CLIENT {get_ip(client)} app_name: {app_name}")
				
			if message_type == CmdPacketType.SYNC_NEXT_MESSAGE.name and not client_fwd[ip] and forwarding:
				logger.debug(f"CLIENT {ipclientname} starting message-sync ...")
				client_fwd_active[ip] = True

				data = db_load_packet(ip, get_client_name(client))
				if data:
					msg_count = len(data)
					logger.info(f"CLIENT {ipclientname} Forward {msg_count} stored messages")
					for packet in data:
						logger.debug(f"CLIENT {ipclientname} Forward stored data: {packet}")
						client.send(packet)
						
						message = client.recv(1024)
						logger.debug(f"CLIENT {ipclientname} responds: {message}")
				else:
					logger.info(f"CLIENT {ipclientname} no stored messages to forward")
				
				client_fwd[ip] = True
				client_fwd_active[ip] = False
				logger.info(f"CLIENT {ipclientname} finished message-sync")
			
			if client_fwd[ip] and forwarding:
				db_set_client_time(ip, get_client_name(client))
			
			device_write(message)
		
		except (socket.error, BrokenPipeError):
			logger.error(f"CLIENT {get_ip_client_name(device)} lost connection on read")
			if client in clients:
				index = clients.index(client)
			clients.remove(client)
			client.close()
			break

def client_connect(server_sock,forwarding = False):
	global clients, client_names, threads, client_fwd, stop_device
	
	if forwarding:
		info = 'for store-forward '
	else:
		info = ''
	
	while not stop_device:
		ip, port = server_sock.getsockname()
		logger.info(f"CLIENT: listening at {ip}:{port} {info}...")
		
		try:
			client, address = server_sock.accept()
			ip = get_ip(client)
			logger.info(f"CLIENT: new connection from {ip}")
			
			clients.append(client)
			
			client_fwd[ip] = False
			client_fwd_active[ip] = False
			
			thread = threading.Thread(target=client_receive, args=(client, forwarding, ), name="CLIENT "+ip)
			thread.start()
			threads.append(thread)
		
		except KeyboardInterrupt:
			logger.info(f"EXIT after KeyboardInterrupt ...")
			stop_device = True
			
			for t in threads:
				logger.debug(f"EXIT: waiting for thread {t.name} to finish ...")
				t.join()
				
			sys.exit()

def device_state(running_threads):
	while True:
		for thread in running_threads:
			if thread.name == "DEVICE_HANDLE":
				print('device_state',thread.is_alive())
				if not thread.is_alive():
					print('device failed')
					os.kill(os.getpid(), signal.SIGINT)

		time.sleep(2)

parser = argparse.ArgumentParser(description='TCP meshcore proxy')
parser.add_argument('-s', '--server', required=True, help='Server IP and port, i.e.: 127.0.0.1:5000')
parser.add_argument('-f', '--forward', required=False, help='Server IP and port for clients using message storage, i.e.: 127.0.0.1:5001')
parser.add_argument('-d', '--device', required=True, help='Device IP and port, i.e.: 127.0.0.2:5000')
parser.add_argument('-sql', '--sqlite', action='store_true', help='enable SQLite-function')
parser.add_argument('-ver', '--version', action='store_true', help='shows current app-version')
output_group = parser.add_mutually_exclusive_group()
output_group.add_argument('-q', '--quiet', action='store_true', help='minimal logging to CLI')
output_group.add_argument('-v', '--verbose', action='store_true', help='maximum logging to CLI')
args = parser.parse_args()

client_host, client_port = ip_to_tuple(args.server)
device_host, device_port = ip_to_tuple(args.device)

if args.forward:
	forward_host, forward_port = ip_to_tuple(args.forward)

logger.info(f"{APP} v{VERSION}")

if args.version:
	sys.exit()
if args.quiet:
	logger.setLevel(logging.CRITICAL)
if args.verbose:
	logger.setLevel(logging.DEBUG)
if args.sqlite:
	logger.info('SQLITE: enable message storage')
	sqlite_active = True

if sqlite_active:
	logger.debug(f"SQLITE: using local database file at {sqlite_file} ...")
	if not os.path.exists(sqlite_file):
		db_create()

if logger.getEffectiveLevel() == logging.DEBUG:
	logger.info('LOGLEVEL is set to DEBUG')

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((client_host,client_port))
#logger.info(f"CLIENT: listening at {client_host}:{client_port} ...")
server.listen()

if sqlite_active:
	server2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	server2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	server2.bind((forward_host,forward_port))
	server2.listen()

device_handle_thread = threading.Thread(target=device_handle, name="DEVICE_HANDLE")
device_handle_thread.start()
threads.append(device_handle_thread)

if sqlite_active:
	logger.debug(f"CLIENT: waiting for forward-clients to connect ...")
	forward_handle_thread = threading.Thread(target=client_connect, args=(server2,True,), kwargs={}, name="FORWARD_HANDLE")
	threads.append(forward_handle_thread)
	forward_handle_thread.start()

logger.debug(f"CLIENT: waiting for clients to connect ...")
client_connect(server)
