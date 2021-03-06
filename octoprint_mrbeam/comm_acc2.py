# coding=utf-8
from __future__ import absolute_import
__author__ = "Florian Becker <florian@mr-beam.org> based on work by Gina Häußge and David Braam"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2013 David Braam - Released under terms of the AGPLv3 License"

import os
import threading
import logging
import glob
import time
import datetime
import serial
import re
import Queue

from yaml import load as yamlload
from yaml import dump as yamldump
from subprocess import call as subprocesscall

import octoprint.plugin

from .profile import laserCutterProfileManager

from octoprint.settings import settings, default_settings
from octoprint.events import eventManager, Events as OctoPrintEvents
from octoprint.filemanager.destinations import FileDestinations
from octoprint.util import get_exception_string, RepeatedTimer, CountedEvent, sanitize_ascii

from octoprint_mrbeam.mrb_logger import mrb_logger
from octoprint_mrbeam.analytics.analytics_handler import existing_analyticsHandler
from octoprint_mrbeam.util.cmd_exec import exec_cmd_output

### MachineCom #########################################################################################################
class MachineCom(object):


	### GRBL VERSIONs #######################################
	# original grbl
	GRBL_VERSION_20170919_22270fa = '0.9g_22270fa'
	#
	# adds rescue from home feature
	GRBL_VERSION_20180223_61638c5 = '0.9g_20180223_61638c5'
	GRBL_FEAT_BLOCK_VERSION_LIST_RESCUE_FROM_HOME = (GRBL_VERSION_20170919_22270fa)
	#
	# trial grbl
	# - adds rx-buffer state with every ok
	# - adds alarm mesage on rx buffer overrun
	GRBL_VERSION_20180828_ac367ff = '0.9g_20180828_ac367ff'
	GRBL_FEAT_BLOCK_VERSION_LIST_RX_BUFFER_REPORTING = (GRBL_VERSION_20170919_22270fa, GRBL_VERSION_20180223_61638c5)
	#
	# adds G24_AVOIDED
	GRBL_VERSION_20181116_a437781 = '0.9g_20181116_a437781'
	#
	##########################################################


	GRBL_SETTINGS_READ_WINDOW =     10.0
	GRBL_SETTINGS_CHECK_FREQUENCY = 0.5

	STATE_NONE = 0
	STATE_OPEN_SERIAL = 1
	STATE_DETECT_SERIAL = 2
	STATE_DETECT_BAUDRATE = 3
	STATE_CONNECTING = 4
	STATE_OPERATIONAL = 5
	STATE_PRINTING = 6
	STATE_PAUSED = 7
	STATE_CLOSED = 8
	STATE_ERROR = 9
	STATE_CLOSED_WITH_ERROR = 10
	STATE_TRANSFERING_FILE = 11
	STATE_LOCKED = 12
	STATE_HOMING = 13
	STATE_FLASHING = 14

	GRBL_STATE_QUEUE = 'Queue'
	GRBL_STATE_IDLE  = 'Idle'
	GRBL_STATE_RUN   = 'Run'

	COMMAND_STATUS   = '?'
	COMMAND_HOLD     = '!'
	COMMAND_RESUME   = '~'
	COMMAND_RESET    = b'\x18'
	COMMAND_FLUSH    = 'FLUSH'
	COMMAND_SYNC     = 'SYNC' # experimental

	STATUS_POLL_FREQUENCY_OPERATIONAL = 2.0
	STATUS_POLL_FREQUENCY_PRINTING = 5.0 # set back top 1.0 if it's not causing gcode24
	STATUS_POLL_FREQUENCY_PAUSED = 0.2
	STATUS_POLL_FREQUENCY_SYNCING = 0.2
	STATUS_POLL_FREQUENCY_DEFAULT = STATUS_POLL_FREQUENCY_PRINTING

	GRBL_SYNC_COMMAND_WAIT_STATES = (GRBL_STATE_RUN, GRBL_STATE_QUEUE)
	GRBL_HEX_FOLDER = 'files/grbl/'

	pattern_grbl_status_legacy = re.compile("<(?P<status>\w+),.*MPos:(?P<mpos_x>[0-9.\-]+),(?P<mpos_y>[0-9.\-]+),.*WPos:(?P<pos_x>[0-9.\-]+),(?P<pos_y>[0-9.\-]+),.*RX:(?P<rx>\d+),.*laser (?P<laser_state>\w+):(?P<laser_intensity>\d+).*>")
	pattern_grbl_status = re.compile("<(?P<status>\w+),.*MPos:(?P<mpos_x>[0-9.\-]+),(?P<mpos_y>[0-9.\-]+),.*WPos:(?P<pos_x>[0-9.\-]+),(?P<pos_y>[0-9.\-]+),.*RX:(?P<rx>\d+),.*limits:(?P<limit_x>[x]?)(?P<limit_y>[y]?)z?,.*laser (?P<laser_state>\w+):(?P<laser_intensity>\d+).*>")
	pattern_grbl_version = re.compile("Grbl (?P<version>\S+)\s.*")
	pattern_grbl_setting = re.compile("\$(?P<id>\d+)=(?P<value>\S+)\s\((?P<comment>.*)\)")


	ALARM_CODE_COMMAND_TOO_LONG = "ALARM_CODE_COMMAND_TOO_LONG"
  
	pattern_get_x_coord_from_gcode = re.compile("^G.*X(\d{1,3}\.?\d{0,3})\D.*")
	pattern_get_y_coord_from_gcode = re.compile("^G.*Y(\d{1,3}\.?\d{0,3})\D.*")

	def __init__(self, port=None, baudrate=None, callbackObject=None, printerProfileManager=None):
		self._logger = mrb_logger("octoprint.plugins.mrbeam.comm_acc2")
		self._serialLogger = logging.getLogger("SERIAL")

		if port is None:
			port = settings().get(["serial", "port"])
		elif isinstance(port, list):
			port = port[0]
		if baudrate is None:
			settingsBaudrate = settings().getInt(["serial", "baudrate"])
			if settingsBaudrate is None:
				baudrate = 0
			else:
				baudrate = settingsBaudrate
		if callbackObject is None:
			callbackObject = MachineComPrintCallback()

		self._port = port
		self._baudrate = baudrate
		self._callback = callbackObject
		self._laserCutterProfile = laserCutterProfileManager().get_current_or_default()

		self.RX_BUFFER_SIZE = 127
		self.WORKING_RX_BUFFER_SIZE = self.RX_BUFFER_SIZE - 5

		self._state = self.STATE_NONE
		self._grbl_state = None
		self._grbl_version = None
		self._grbl_settings = dict()
		self._errorValue = "Unknown Error"
		self._serial = None
		self._currentFile = None
		self._status_polling_timer = None
		self._status_polling_next_ts = 0
		self._status_polling_interval = self.STATUS_POLL_FREQUENCY_DEFAULT
		self._acc_line_buffer = []
		self._last_acknowledged_command = None
		self._last_wx = -1
		self._last_wy = -1
		self._cmd = None
		self._pauseWaitStartTime = None
		self._pauseWaitTimeLost = 0.0
		self._commandQueue = Queue.Queue()
		self._send_event = CountedEvent(max=50)
		self._finished_currentFile = False
		self._pause_delay_time = 0
		self._feedrate_factor = 1
		self._actual_feedrate = None
		self._intensity_factor = 1
		self._actual_intensity = None
		self._feedrate_dict = {}
		self._intensity_dict = {}
		self._passes = 1
		self._finished_passes = 0
		self._sync_command_ts = -1
		self._sync_command_state_sent = False
		self.limit_x = -1
		self.limit_y = -1
		# from GRBL status RX value: Number of characters queued in Grbl's serial RX receive buffer.
		self._grbl_rx_status = -1
		self._grbl_settings_correction_ts = 0
		self._rx_stats = RxBufferStats()

		self.g24_avoided_message = []

		self.grbl_auto_update_enabled = _mrbeam_plugin_implementation._settings.get(['dev', 'grbl_auto_update_enabled'])

		#grbl features
		self.grbl_feat_rescue_from_home = False
		self.grbl_feat_report_rx_buffer_state = False

		# regular expressions
		self._regex_command = re.compile("^\s*\$?([GM]\d+|[THFSX])")
		self._regex_feedrate = re.compile("F\d+", re.IGNORECASE)
		self._regex_intensity = re.compile("S\d+", re.IGNORECASE)

		self._real_time_commands={'poll_status':False,
								'feed_hold':False,
								'cycle_start':False,
								'soft_reset':False}

		# hooks
		self._pluginManager = octoprint.plugin.plugin_manager()
		self._serial_factory_hooks = self._pluginManager.get_hooks("octoprint.comm.transport.serial.factory")

		# threads
		self.monitoring_thread = None
		self.sending_thread = None
		self._start_monitoring_thread()
		self._start_status_polling_timer()


	def _start_monitoring_thread(self):
		self._monitoring_active = True
		self.monitoring_thread = threading.Thread(target=self._monitor_loop, name="comm._monitoring_thread")
		self.monitoring_thread.daemon = True
		self.monitoring_thread.start()

	def _start_sending_thread(self):
		self._sending_active = True
		self.sending_thread = threading.Thread(target=self._send_loop, name="comm._sending_thread")
		self.sending_thread.daemon = True
		self.sending_thread.start()

	def _start_status_polling_timer(self):
		if self._status_polling_timer is not None:
			self._status_polling_timer.cancel()
		self._status_polling_timer = RepeatedTimer(0.1, self._poll_status)
		self._status_polling_timer.start()


	def get_home_position(self):
		"""
		Returns the home position which usually where the head is after homing. (Except in C series)
		:return: Tuple of (x, y) position
		"""
		if self._laserCutterProfile['legacy']['job_done_home_position_x'] is not None:
			return (self._laserCutterProfile['legacy']['job_done_home_position_x'],
			        self._laserCutterProfile['volume']['depth'] + self._laserCutterProfile['volume']['working_area_shift_y'])
		return (self._laserCutterProfile['volume']['width'] + self._laserCutterProfile['volume']['working_area_shift_x'], # x
		        self._laserCutterProfile['volume']['depth'] + self._laserCutterProfile['volume']['working_area_shift_y']) # y

	def _monitor_loop(self):
		#Open the serial port.
		if not self._openSerial():
			self._logger.critical("_monitor_loop() Serial not open, leaving monitoring loop.")
			return

		self._logger.info("Connected to: %s, starting monitor" % self._serial, terminal_as_comm=True)
		self._changeState(self.STATE_CONNECTING)

		if self.grbl_auto_update_enabled and self._laserCutterProfile['grbl']['auto_update_file']:
			self._logger.info("GRBL auto updating to version: %s, file: %s", self._laserCutterProfile['grbl']['auto_update_version'], self._laserCutterProfile['grbl']['auto_update_file'])
			self.flash_grbl(grbl_file=self._laserCutterProfile['grbl']['auto_update_file'], is_connected=False)

		# reset on connect
		if self._laserCutterProfile['grbl']['resetOnConnect']:
			self._serial.flushInput()
			self._serial.flushOutput()
			self._sendCommand(self.COMMAND_RESET)
		self._timeout = get_new_timeout("communication")

		while self._monitoring_active:
			try:
				line = self._readline()
				if line is None:
					break
				if line.strip() is not "":
					self._timeout = get_new_timeout("communication")
				if line.startswith('<'): # status report
					self._handle_status_report(line)
				elif line.startswith('ok'): # ok message :)
					self._handle_ok_message(line)
				elif line.startswith('err'): # error message
					self._handle_error_message(line)
				elif line.startswith('ALA'): # ALARM message
					self._handle_alarm_message(line)
				elif line.startswith('['): # feedback message
					self._handle_feedback_message(line)
				elif line.startswith('Grb'): # Grbl startup message
					self._handle_startup_message(line)
				# elif line.startswith('G24_AVOIDED'):
				# 	self._handle_g24_avoided_message(line)
				elif line.startswith('Corru'): # Corrupted line:
					self._handle_corrupted_line(line)
				elif line.startswith('$'): # Grbl settings
					self._handle_settings_message(line)
				elif not line and (self._state is self.STATE_CONNECTING or self._state is self.STATE_OPEN_SERIAL or self._state is self.STATE_DETECT_SERIAL):
					self._logger.info("Empty line received during STATE_CONNECTION, starting soft-reset", terminal_as_comm=True)
					self._sendCommand(self.COMMAND_RESET) # Serial-Connection Error
			except:
				self._logger.exception("Something crashed inside the monitoring loop, please report this to Mr Beam")
				errorMsg = "See octoprint.log for details"
				self._log(errorMsg)
				self._errorValue = errorMsg
				self._changeState(self.STATE_ERROR)
				eventManager().fire(OctoPrintEvents.ERROR, {"error": self.getErrorString()})
				self._logger.dump_terminal_buffer(level=logging.ERROR)
		self._logger.info("Connection closed, closing down monitor", terminal_as_comm=True)

	def _send_loop(self):
		while self._sending_active:
			try:
				self._process_rt_commands()
				if self.isPrinting() and self._commandQueue.empty():
					cmd = self._getNext()
					if cmd is not None:
						self.sendCommand(cmd)
						self._callback.on_comm_progress()
					else:
						if self._finished_passes >= self._passes:
							if len(self._acc_line_buffer) == 0:
								self._set_print_finished()
						self._currentFile.resetToBeginning()
						cmd = self._getNext()
						if cmd is not None:
							self.sendCommand(cmd)
							self._callback.on_comm_progress()

				self._sendCommand()
				self._send_event.wait(1)
				self._send_event.clear()
			except:
				self._logger.exception("Something crashed inside the sending loop, please report this to Mr Beam.")
				errorMsg = "See octoprint.log for details"
				self._log(errorMsg)
				self._errorValue = errorMsg
				self._changeState(self.STATE_ERROR)
				eventManager().fire(OctoPrintEvents.ERROR, {"error": self.getErrorString()})
				self._logger.dump_terminal_buffer(level=logging.ERROR)
		# self._logger.info("ANDYTEST Leaving _send_loop()")

	def _sendCommand(self, cmd=None):
		if cmd is None:
			if self._cmd is None and self._commandQueue.empty():
				return
			elif self._cmd is None:
				self._cmd = self._commandQueue.get()


			if self._cmd == self.COMMAND_FLUSH:
				# FLUSH waits until we're no longer waiting for any OKs from GRBL
				if self._sync_command_ts <=0:
					self._sync_command_ts = time.time()
					self._log("FLUSHing (grbl_state: {}, acc_line_buffer: {}, grbl_rx: {})".format(
					                  self._grbl_state, sum([len(x) for x in self._acc_line_buffer]), self._grbl_rx_status))
				if len(self._acc_line_buffer) <= 0:
					self._cmd = None
					self._log("FLUSHed ({}ms)".format(int(1000*(time.time() - self._sync_command_ts))))
					self._sync_command_ts = -1
				self._send_event.set()
			elif self._cmd == self.COMMAND_SYNC:
				# SYNC waits until we're no longer waiting for any OKs from GRBL and GRBL has reported to no be busy anymore.
				# Still experimential: We need to test/verify/implement:
				# - Maybe we need to turn off the laser here...
				# - What if RX buffer remains 1 and never becomes 0? sync should handle/correct this
				# - Do we need a timeout or something?
				if self._sync_command_ts <=0:
					self._sync_command_ts = time.time()
					self._log("SYNCing (grbl_state: {}, acc_line_buffer: {}, grbl_rx: {})".format(
					                  self._grbl_state, sum([len(x) for x in self._acc_line_buffer]), self._grbl_rx_status))
				if len(self._acc_line_buffer) <= 0 and not self._grbl_state in self.GRBL_SYNC_COMMAND_WAIT_STATES:
					# Successfully synced, let's move on
					self._cmd = None
					self._log("SYNCed ({}ms)".format(int(1000*(time.time() - self._sync_command_ts))))
					self._sync_command_ts = -1
					self._sync_command_state_sent = False
				elif len(self._acc_line_buffer) <= 0 and self._grbl_state in self.GRBL_SYNC_COMMAND_WAIT_STATES and not self._sync_command_state_sent:
					# Request a status update from GRBL to see if it's really ready.
					self._sync_command_state_sent = True
					self._sendCommand(self.COMMAND_STATUS)
				self._send_event.set()
			else:
				my_cmd = self._cmd  # to avoid race conditions
				if not(len(my_cmd) +1 < self.WORKING_RX_BUFFER_SIZE):
					msg = "Error: Command too long. max: {}, cmd length: {}, cmd: {}... (shortened)".format(self.WORKING_RX_BUFFER_SIZE -1, len(my_cmd), my_cmd[0:self.WORKING_RX_BUFFER_SIZE-1])
					self._logger.error(msg)
					self._handle_alarm_message("Command too long to send to GRBL.", code=self.ALARM_CODE_COMMAND_TOO_LONG)
					self._cmd = None
					return
				if sum([len(x) for x in self._acc_line_buffer]) + len(my_cmd) +1 < self.WORKING_RX_BUFFER_SIZE:
					my_cmd, _, _  = self._process_command_phase("sending", my_cmd)
					self._log("Send: %s" % my_cmd)
					self._acc_line_buffer.append(my_cmd + '\n')
					try:
						self._serial.write(my_cmd + '\n')
						self._process_command_phase("sent", my_cmd)
						self._cmd = None
						self._send_event.set()
					except serial.SerialException:
						self._log("Unexpected error while writing serial port: %s" % (get_exception_string()))
						self._errorValue = get_exception_string()
						self.close(True)
		else:
			cmd, _, _  = self._process_command_phase("sending", cmd)
			self._log("Send: %s" % cmd)
			try:

				self._serial.write(cmd)
				self._process_command_phase("sent", cmd)
			except serial.SerialException:
				self._logger.info("Unexpected error while writing serial port: %s" % (get_exception_string()), terminal_as_comm=True)
				self._errorValue = get_exception_string()
				self.close(True)

	def _process_rt_commands(self):
		if self._real_time_commands['poll_status']:
			self._sendCommand(self.COMMAND_STATUS)
			self._real_time_commands['poll_status']=False
		elif self._real_time_commands['feed_hold']:
			self._sendCommand(self.COMMAND_HOLD)
			self._real_time_commands['feed_hold']=False
		elif self._real_time_commands['cycle_start']:
			self._sendCommand(self.COMMAND_RESUME)
			self._real_time_commands['cycle_start']=False
		elif self._real_time_commands['soft_reset']:
			self._sendCommand(self.COMMAND_RESET)
			self._real_time_commands['soft_reset']=False

	def _openSerial(self):
		self._grbl_version = None
		self._grbl_settings = dict()

		def default(_, port, baudrate, read_timeout):
			if port is None or port == 'AUTO':
				# no known port, try auto detection
				self._changeState(self.STATE_DETECT_SERIAL)
				ser = self._detectPort(True)
				if ser is None:
					self._errorValue = 'Failed to autodetect serial port, please set it manually.'
					self._changeState(self.STATE_ERROR)
					eventManager().fire(OctoPrintEvents.ERROR, {"error": self.getErrorString()})
					self._log("Failed to autodetect serial port, please set it manually.")
					self._logger.dump_terminal_buffer(level=logging.ERROR)
					return None
				port = ser.port

			# connect to regular serial port
			self._logger.info("Connecting to: %s" % port, terminal_as_comm=True)
			if baudrate == 0:
				baudrates = baudrateList()
				ser = serial.Serial(str(port), 115200 if 115200 in baudrates else baudrates[0], timeout=read_timeout, writeTimeout=10000, parity=serial.PARITY_ODD)
			else:
				ser = serial.Serial(str(port), baudrate, timeout=read_timeout, writeTimeout=10000, parity=serial.PARITY_ODD)
			ser.close()
			ser.parity = serial.PARITY_NONE
			ser.open()
			return ser

		serial_factories = self._serial_factory_hooks.items() + [("default", default)]
		for name, factory in serial_factories:
			try:
				serial_obj = factory(self, self._port, self._baudrate, settings().getFloat(["serial", "timeout", "connection"]))
			except (OSError, serial.SerialException):
				exception_string = get_exception_string()
				self._errorValue = "Connection error, see Terminal tab"
				self._changeState(self.STATE_ERROR)
				eventManager().fire(OctoPrintEvents.ERROR, {"error": self.getErrorString()})
				self._log("Unexpected error while connecting to serial port: %s %s (hook %s)" % (self._port, exception_string, name))
				if "failed to set custom baud rate" in exception_string.lower():
					self._log("Your installation does not support custom baudrates (e.g. 250000) for connecting to your printer. This is a problem of the pyserial library that OctoPrint depends on. Please update to a pyserial version that supports your baudrate or switch your printer's firmware to a standard baudrate (e.g. 115200). See https://github.com/foosel/OctoPrint/wiki/OctoPrint-support-for-250000-baud-rate-on-Raspbian")
				self._logger.dump_terminal_buffer(level=logging.ERROR)
				return False
			if serial_obj is not None:
				# first hook to succeed wins, but any can pass on to the next
				self._changeState(self.STATE_OPEN_SERIAL)
				self._serial = serial_obj
				return True
		return False

	def _readline(self):
		if self._serial is None:
			return None
		ret = None
		try:
			ret = self._serial.readline()
			self._send_event.set()
			if('ok' in ret or 'error' in ret):
				if(len(self._acc_line_buffer) > 0):
					self._last_acknowledged_command = self._acc_line_buffer[0]
					del self._acc_line_buffer[0]  # Delete the commands character count corresponding to the last 'ok'
				else:
					self._logger.error("Received OK but internal _acc_line_buffer counter is empty.")
		except serial.SerialException:
			self._logger.error("Unexpected error while reading serial port: %s" % (get_exception_string()), terminal_as_comm=True)
			self._errorValue = get_exception_string()
			self.close(True)
			return None
		except TypeError:
			# While closing or reopening sometimes we get this exception:
			# 	File "build/bdist.linux-armv7l/egg/serial/serialposix.py", line 468, in read
	        #     buf = os.read(self.fd, size-len(read))
			self._logger.exception("TypeError in _readline. Did this happen while closing or re-openting serial?", terminal_as_comm=True)
			pass
		if ret is None or ret == '': return ''
		try:
			self._log("Recv: %s" % sanitize_ascii(ret))
		except ValueError as e:
			# self._log("WARN: While reading last line: %s" % e)
			self._logger.warn("Exception while sanitizing ascii intput from grbl. Excpetion: '%s', original string from grbl: '%s'", e, ret)
			self._log("Recv: %r" % ret)
		return ret

	def _getNext(self):
		if self._finished_currentFile is False:
			line = self._currentFile.getNext()
			if line is None:
				self._finished_passes += 1
				if self._finished_passes >= self._passes:
					self._finished_currentFile = True
			return line
		else:
			return None

	def _set_print_finished(self):
		self._callback.on_comm_print_job_done()
		self._changeState(self.STATE_OPERATIONAL)
		payload = {
			"file": self._currentFile.getFilename(),
			"filename": os.path.basename(self._currentFile.getFilename()),
			"origin": self._currentFile.getFileLocation(),
			"time": self.getPrintTime(),
			"mrb_state": _mrbeam_plugin_implementation.get_mrb_state()
		}
		self._move_home()
		eventManager().fire(OctoPrintEvents.PRINT_DONE, payload)
		self._logger.info(self._rx_stats.pp(print_time=self.getPrintTime()))

	def _move_home(self):
		self.sendCommand("M5")
		h_pos = self.get_home_position()
		command = "G0X{x}Y{y}".format(x=h_pos[0], y=h_pos[1])
		self.sendCommand(command)
		self.sendCommand("M9")

	def _handle_status_report(self, line):
		match = None
		if (self._grbl_version == self.GRBL_VERSION_20170919_22270fa):
			match = self.pattern_grbl_status_legacy.match(line)
		else:
			match = self.pattern_grbl_status.match(line)
		if not match:
			self._logger.warn("GRBL status string did not match pattern. GRBL version: %s, status string: %s", self._grbl_version, line)
			return

		groups = match.groupdict()
		self._grbl_state = groups['status']

		#  limit (end stops) not supported in legacy GRBL version
		if 'limit_x' in groups: self.limit_x = time.time() if groups['limit_x'] else 0
		if 'limit_y' in groups: self.limit_y = time.time() if groups['limit_y'] else 0

		# grbl_character_buffer
		if 'rx' in groups: self._grbl_rx_status = groups['rx'] if groups['rx'] else -1

		# positions
		try:
			self.MPosX = float(groups['mpos_x'])
			self.MPosY = float(groups['mpos_y'])
			wx = float(groups['pos_x'])
			wy = float(groups['pos_y'])
			self._last_wx = wx
			self._last_wy = wy
			self._callback.on_comm_pos_update([self.MPosX, self.MPosY, 0], [wx, wy, 0])
		except:
			self._logger.exception("Exception while handling position updates from GRBL.")

		# laser
		self._handle_laser_intensity_for_analytics(groups['laser_state'], groups['laser_intensity'])

		# unintended pause....
		if self._grbl_state == self.GRBL_STATE_QUEUE:
			if time.time() - self._pause_delay_time > 0.3:
				if not self.isPaused():
					if _mrbeam_plugin_implementation and _mrbeam_plugin_implementation._oneButtonHandler and \
						not _mrbeam_plugin_implementation._oneButtonHandler.is_intended_pause():
						self._logger.warn("_handle_status_report() Override pause since we got status '%s' from grbl.", self._grbl_state)
						self.setPause(False, send_cmd=True, force=True, trigger="GRBL_QUEUE_OVERRIDE")
					else:
						self._logger.warn("_handle_status_report() Pausing since we got status '%s' from grbl.", self._grbl_state)
						self.setPause(True, send_cmd=False, trigger="GRBL_QUEUE")
						self._logger.dump_terminal_buffer(logging.WARN)
		elif self._grbl_state == self.GRBL_STATE_RUN or self._grbl_state == self.GRBL_STATE_IDLE:
			if time.time() - self._pause_delay_time > 0.3:
				if self.isPaused():
					self._logger.warn("_handle_status_report() Unpausing since we got status '%s' from grbl.", self._grbl_state)
					self.setPause(False, send_cmd=False, trigger="GRBL_RUN")


	def _handle_laser_intensity_for_analytics(self, laser_state, laser_intensity):
		if laser_state == 'on':
			analytics = existing_analyticsHandler()
			if analytics:
				analytics.add_laser_intensity_value(int(laser_intensity))


	def _handle_ok_message(self, line):
		if self._state == self.STATE_HOMING:
			self._changeState(self.STATE_OPERATIONAL)

		# update working pos from acknowledged gcode
		my_cmd = self._last_acknowledged_command
		if my_cmd.startswith('G'):
			x_pos = self._last_wx
			y_pos = self._last_wy
			match = self.pattern_get_x_coord_from_gcode.match(my_cmd)
			if match:
				try:
					x_pos = float(match.group(1))
					self._last_wx = x_pos
				except:
					self._logger.exception("Exception in parsing x coord from gcode. my_cmd: '%s' ", my_cmd)
					pass

			match = self.pattern_get_y_coord_from_gcode.match(my_cmd)
			if match:
				try:
					y_pos = float(match.group(1))
					self._last_wy = y_pos
				except:
					self._logger.exception("Exception in parsing y coord from gcode. my_cmd: '%s' ", my_cmd)
					pass

			if x_pos >= 0 or y_pos >= 0:
				self._callback.on_comm_pos_update(None, [x_pos, y_pos, 0])
				# since we just got a postion update we can reset the wait time for the next status poll
				# ideally we never poll statuses during engravings
				self._reset_status_polling_waittime()

		if self.grbl_feat_report_rx_buffer_state:
			# important that we add every call and count the invalid values internally!
			rx_free = None
			try:
				if line is not None:
					rx_free = int(line.split(':')[1]) # ok:127
			except e:
				self._logger.warn("_handle_ok_message() Can't read free_rx_bytes from line: '%s', error: %s", line, e)
			self._rx_stats.add(rx_free)

	def _handle_error_message(self, line):
		"""
		Handles error messages from GRBL
		:param line: GRBL error respnse
		"""
		# grbl repots an error if there was never any data written to it's eeprom.
		# it's going to write default values to eeprom and everything is fine then....
		if "EEPROM read fail" in line:
			self._logger.debug("_handle_error_message() Ignoring this error message: '%s'", line)
			return
		self._logger.error("_handle_error(): %s", line)
		self._errorValue = line
		eventManager().fire(OctoPrintEvents.ERROR, {"error": self.getErrorString()})
		self._changeState(self.STATE_LOCKED)
		self._logger.dump_terminal_buffer(level=logging.ERROR)
		self._rx_stats.pp(print_time=self.getPrintTime())

	def _handle_alarm_message(self, line, code=None):
		errorMsg = None
		throwErrorMessage = True
		dumpTerminal = True
		if code == self.ALARM_CODE_COMMAND_TOO_LONG:
			# this is not really a GRBL alarm state. Hacky to have it handled as one...
			errorMsg = line or str(self.ALARM_CODE_COMMAND_TOO_LONG)
			dumpTerminal = False
		elif "Hard/soft limit" in line:
			errorMsg = "Machine Limit Hit. Please reset the machine and do a homing cycle"
		elif "Abort during cycle" in line:
			errorMsg = "Soft-reset detected. Please do a homing cycle"
			throwErrorMessage = False
		elif "Probe fail" in line:
			errorMsg = "Probing has failed. Please reset the machine and do a homing cycle"
		elif "Probe fail" in line:
			errorMsg = "Probing has failed. Please reset the machine and do a homing cycle"
		else:
			errorMsg = "GRBL alarm message: '{}'".format(line)

		if errorMsg:
			self._log(errorMsg)
			self._errorValue = errorMsg
			if throwErrorMessage:
				eventManager().fire(OctoPrintEvents.ERROR, {"error": self.getErrorString()})
			if dumpTerminal:
				self._logger.dump_terminal_buffer(level=logging.ERROR)

		with self._commandQueue.mutex:
			self._commandQueue.queue.clear()
		self._acc_line_buffer = []
		self._send_event.clear(completely=True)
		self._changeState(self.STATE_LOCKED)

		# close and open serial port to reset arduino
		self._serial.close()
		self._openSerial()

	def _handle_feedback_message(self, line):
		if line[1:].startswith('Res'): # [Reset to continue]
			#send ctrl-x back immediately '\x18' == ctrl-x
			self._serial.write(list(bytearray('\x18')))
			pass
		elif line[1:].startswith('\'$H'): # ['$H'|'$X' to unlock]
			self._changeState(self.STATE_LOCKED)
			if self.isOperational():
				errorMsg = "Machine reset."
				self._cmd = None
				self._acc_line_buffer = []
				self._pauseWaitStartTime = None
				self._pauseWaitTimeLost = 0.0
				self._send_event.clear(completely=True)
				with self._commandQueue.mutex:
					self._commandQueue.queue.clear()
				self._log(errorMsg)
				self._errorValue = errorMsg
				eventManager().fire(OctoPrintEvents.ERROR, {"error": self.getErrorString()})
				self._logger.dump_terminal_buffer(level=logging.ERROR)
		elif line[1:].startswith('G24'): # [G24_AVOIDED]
			self.g24_avoided_message = []
			self._logger.warn("G24_AVOIDED (Corrupted line data will follow)")
		elif line[1:].startswith('Cau'): # [Caution: Unlocked]
			pass
		elif line[1:].startswith('Ena'): # [Enabled]
			pass
		elif line[1:].startswith('Dis'): # [Disabled]
			pass

	def _handle_startup_message(self, line):
		match = self.pattern_grbl_version.match(line)
		if match:
			self._grbl_version = match.group('version')
		else:
			self._logger.error("Unable to parse GRBL version from startup message: ", line)

		self.grbl_feat_rescue_from_home = self._grbl_version not in self.GRBL_FEAT_BLOCK_VERSION_LIST_RESCUE_FROM_HOME
		self.grbl_feat_report_rx_buffer_state = self._grbl_version not in self.GRBL_FEAT_BLOCK_VERSION_LIST_RX_BUFFER_REPORTING
		self.reset_grbl_auto_update_config()

		self._logger.info("GRBL version: %s, rescue_from_home: %s, report_rx_buffer_state: %s, auto_update: %s",
		                  self._grbl_version,
		                  self.grbl_feat_rescue_from_home,
		                  self.grbl_feat_report_rx_buffer_state,
		                  self.grbl_auto_update_enabled)

		self._onConnected(self.STATE_LOCKED)
		self.correct_grbl_settings()


	def _handle_corrupted_line(self, line):
		"""
		So far this 'Corrupted line' is sent only in combination with G24_AVOIDED

		> 11:39:15,866 _COMM_: Send: G1X58.32Y338.49G1X56.78Y338.57
		> ...
		> 11:39:16,786 _COMM_: Recv: [G24_AVOIDED]
		> 11:39:16,789 _COMM_: Recv: Corrupted line: G1X58.32Y338.49
		> ...
		> 11:39:17,221 _COMM_: Recv: Corrupted line: G1X56.78Y338.57

		Rsults in this output:
		# WARNING - G24_AVOIDED line: 'G1X58.32Y338.49G1X56.78Y338.57' (hex: [47 31 58 35 38 2E 33 32 59 33 33 38 2E 34 39][47 31 58 35 36 2E 37 38 59 33 33 38 2E 35 37])
		:param line:
		"""
		data = line[len('Corrupted line: '):]
		self.g24_avoided_message.append(data)
		if len(self.g24_avoided_message) >= 2:
			self.send_g24_avoided_message()
			self.g24_avoided_message = []


	def send_g24_avoided_message(self):
		try:
			data_str = ''
			data_hex = ''
			for i in self.g24_avoided_message:
				if i.endswith('\n'):
					# A line always ends with a \n which is added by GRBL to evey line sent back to us.
					i = i[:-1]
				data_str += i
				data_hex += '[{}]'.format(self.get_hex_str_from_str(i))

			self._logger.warn("G24_AVOIDED line: '%s' (hex: %s)",data_str, data_hex)
			self._logger.dump_terminal_buffer(level=logging.WARN)

			import urllib
			import cgi
			text = "<br/>"\
			       "An internal event happened which we would love to track and analyse. Please help us improve Mr Beam II by sharing the data below. Thank you for the support.<br/><br/>"\
				    "Please send us the event details per email:<br/>" \
			       "Either simply <strong><a href='mailto:{email_addr}?subject={email_subject}&body={email_body}' target='_blank'>click here</a></strong><br/>" \
			       "or copy the data below into an email and send it to {email_addr}.<br/><br/>" \
			       "Event details:<br>{reason}<br/><br/>"\
				       .format(reason="G24_AVOIDED: {} ({})".format(cgi.escape(data_str, True), data_hex),
	                           email_addr="support@mr-beam.org",
	                           email_subject="G24_AVOIDED",
	                           email_body=urllib.quote_plus("G24_AVOIDED: {} ({})\n\nJust send this email as it is, no need to add extra comments.\nThank you for your help.\n".format(data_str, data_hex))
			                   )
			_mrbeam_plugin_implementation.notify_frontend(
				title="Help our Developers",
				text=text,
				type='warn',
				sticky=True)
		except:
			self._logger.exception("G24_AVOIDED Exception in _handle_g24_avoided_message(): ")

	def _handle_settings_message(self, line):
		"""
		Handles grbl settings message like '$130=515.1'
		:param line:
		"""
		match = self.pattern_grbl_setting.match(line)
		if match:
			id = int(match.group('id'))
			comment = match.group('comment')
			v_str = match.group('value')
			v = float(v_str)
			try:
				i = int(v)
			except ValueError:
				pass
			value = v
			if i == v and v_str.find('.') < 0:
				value = i
			self._grbl_settings[id] = dict(value=value, comment=comment)
		else:
			self._logger.error("_handle_settings_message() line did not mach pattern: %s", line)


	def correct_grbl_settings(self, retries=3):
		"""
		This triggers a reload of GRBL settings and does a validation and correction afterwards.
		"""
		if time.time() - self._grbl_settings_correction_ts > self.GRBL_SETTINGS_READ_WINDOW:
			self._grbl_settings_correction_ts = time.time()
			self._refresh_grbl_settings()
			self._verify_and_correct_loaded_grbl_settings(retries=retries, timeout=self.GRBL_SETTINGS_READ_WINDOW, force_thread=True)
		else:
			self._logger.warn("correct_grbl_settings() got called more than once withing %s s. Ignoring this call.", self.GRBL_SETTINGS_READ_WINDOW )

	def _refresh_grbl_settings(self):
		self._grbl_settings = dict()
		self.sendCommand('$$')

	def _get_string_loaded_grbl_settings(self, settings=None):
		my_grbl_settings = settings or self._grbl_settings.copy()  # to avoid race conditions
		log = []
		for id, data in sorted(my_grbl_settings.iteritems()):
			log.append("${id}={val} ({comment})".format(id=id, val=data['value'], comment=data['comment']))
		return "({count}) [{data}]".format(count=len(log), data=', '.join(log))

	def _verify_and_correct_loaded_grbl_settings(self, retries=0, timeout=0.0, force_thread=False):
		settings_count = self._laserCutterProfile['grbl']['settings_count']
		settings_expected = self._laserCutterProfile['grbl']['settings']
		self._logger.debug("GRBL Settings waiting... timeout: %s, settings count: %s", timeout, len(self._grbl_settings))

		if force_thread or (timeout > 0.0 and len(self._grbl_settings) < settings_count):
			timeout = timeout - self.GRBL_SETTINGS_CHECK_FREQUENCY
			myThread = threading.Timer(self.GRBL_SETTINGS_CHECK_FREQUENCY, self._verify_and_correct_loaded_grbl_settings, kwargs=dict(retries=retries, timeout=timeout))
			myThread.daemon = True
			myThread.name = "CommAcc2_GrblSettings"
			myThread.start()
		else:
			my_grbl_settings = self._grbl_settings.copy() # to avoid race conditions

			log = self._get_string_loaded_grbl_settings(settings=my_grbl_settings)

			commands = []
			if len(my_grbl_settings) != settings_count:
				self._logger.error("GRBL Settings count incorrect!! %s settings but should be %s. Writing all settings to grbl.", len(my_grbl_settings), settings_count)
				for id, value in sorted(settings_expected.iteritems()):
					commands.append("${id}={val}".format(id=id, val=value))
			else:
				for id, value in sorted(settings_expected.iteritems()):
					if not id in my_grbl_settings:
						self._logger.error("GRBL Settings $%s - Missing entry! Should be: %s", id, value)
						commands.append("${id}={val}".format(id=id, val=value))
					elif my_grbl_settings[id]['value'] != value:
						self._logger.error("GRBL Settings $%s=%s (%s) - Incorrect value! Should be: %s",
						                   id, my_grbl_settings[id]['value'], my_grbl_settings[id]['comment'], value)
						commands.append("${id}={val}".format(id=id, val=value))

			if len(commands) > 0:
				msg = "GRBL Settings - Verification: FAILED"
				self._logger.warn(msg + " - " + log)
				self._log(msg)
				self._logger.warn("GRBL Settings correcting: %s values", len(commands), terminal_as_comm=True)
				for c in commands:
					self._logger.warn("GRBL Settings correcting value: %s", c, terminal_as_comm=True)
					# flush before and after to make sure grbl can really handle the settings command
					self.sendCommand(self.COMMAND_FLUSH)
					self.sendCommand(c)
					self.sendCommand(self.COMMAND_FLUSH)
				if retries > 0:
					retries -= 1
					wait_time = 2.0
					self._logger.warn("GRBL Settings corrections done. Restarting verification in %s s", wait_time, terminal_as_comm=True)
					time.sleep(wait_time)
					self._logger.warn("GRBL Settings Restarting verification...", terminal_as_comm=True)
					self.correct_grbl_settings(retries=retries)
				else:
					self._logger.warn("GRBL Settings corrections done. No more retries.", terminal_as_comm=True)

			else:
				msg = "GRBL Settings - Verification: OK"
				self._logger.info(msg + " - " + log)
				self._log(msg)

	def _process_command_phase(self, phase, command, command_type=None, gcode=None):
		if phase not in ("queuing", "queued", "sending", "sent"):
			return command, command_type, gcode

		if gcode is None:
			gcode = self._gcode_command_for_cmd(command)

		# if it's a gcode command send it through the specific handler if it exists
		if gcode is not None:
			gcodeHandler = "_gcode_" + gcode + "_" + phase
			if hasattr(self, gcodeHandler):
				handler_result = getattr(self, gcodeHandler)(command, cmd_type=command_type)
				command, command_type, gcode = self._handle_command_handler_result(command, command_type, gcode, handler_result)

		# finally return whatever we resulted on
		return command, command_type, gcode

	# TODO CLEM Inject color
	def setColors(self,currentFileName, colors):
		print ('>>>>>>>>>>>>>>>>>>>|||||||||||||||<<<<<<<<<<<<<<<<<<<<', currentFileName, colors)

	def _gcode_command_for_cmd(self, cmd):
		"""
		Tries to parse the provided ``cmd`` and extract the GCODE command identifier from it (e.g. "G0" for "G0 X10.0").

		Arguments:
		    cmd (str): The command to try to parse.

		Returns:
		    str or None: The GCODE command identifier if it could be parsed, or None if not.
		"""
		if not cmd:
			return None

		if cmd == self.COMMAND_HOLD: return 'Hold'
		if cmd == self.COMMAND_RESUME: return 'Resume'

		gcode = self._regex_command.search(cmd)
		if not gcode:
			return None

		return gcode.group(1)

	# internal state management
	def _changeState(self, newState):
		if self._state == newState:
			return

		self._set_status_polling_interval_for_state(state=newState)

		if newState == self.STATE_CLOSED or newState == self.STATE_CLOSED_WITH_ERROR:
			if self._currentFile is not None:
				self._currentFile.close()
			self._log("entered state closed / closed with error. reseting character counter.")
			self.acc_line_lengths = []

		oldState = self.getStateString()
		self._state = newState
		self._logger.debug('Changing monitoring state from \'%s\' to \'%s\'' % (oldState, self.getStateString()), terminal_as_comm=True)
		self._callback.on_comm_state_change(newState)

	def _onConnected(self, nextState):
		self._serial.timeout = settings().getFloat(["serial", "timeout", "communication"])

		if(nextState is None):
			self._changeState(self.STATE_LOCKED)
		else:
			self._changeState(nextState)

		if self.sending_thread is None or not self.sending_thread.isAlive():
			self._start_sending_thread()

		payload = dict(grbl_version=self._grbl_version, port=self._port, baudrate=self._baudrate)
		eventManager().fire(OctoPrintEvents.CONNECTED, payload)

	def _detectPort(self, close):
		self._log("Serial port list: %s" % (str(serialList())))
		for p in serialList():
			try:
				self._log("Connecting to: %s" % (p))
				serial_obj = serial.Serial(p)
				if close:
					serial_obj.close()
				return serial_obj
			except (OSError, serial.SerialException) as e:
				self._log("Error while connecting to %s: %s" % (p, str(e)))
		return None

	def _poll_status(self):
		"""
		Called by RepeatedTimer self._status_polling_timer every 0.1 secs
		We need to descide here if we should send a status request
		"""
		try:
			if self.isOperational():
				if time.time() >= self._status_polling_next_ts:
					self._real_time_commands['poll_status'] = True
					self._send_event.set()
					self._status_polling_next_ts = time.time() + self._status_polling_interval
		except:
			self._logger.exception("Exception in status polling call: ")

	def _reset_status_polling_waittime(self):
		"""
		Resets wait time till we should do next status polling
		This is typically called after we received an ok with a position
		"""
		self._status_polling_next_ts = time.time() + self._status_polling_interval

	def _set_status_polling_interval_for_state(self, state=None):
		"""
		Sets polling interval according to current state
		:param state: (optional) state, if None, self._state is used
		"""
		state = state or self._state
		if state == self.STATE_PRINTING:
			self._status_polling_interval = self.STATUS_POLL_FREQUENCY_PRINTING
		elif state == self.STATE_OPERATIONAL:
			self._status_polling_interval = self.STATUS_POLL_FREQUENCY_OPERATIONAL
		elif state == self.STATE_PAUSED:
			self._status_polling_interval = self.STATUS_POLL_FREQUENCY_PAUSED

	def _soft_reset(self):
		if self.isOperational():
			self._real_time_commands['soft_reset']=True
			self._send_event.set()

	def _log(self, message):
		# self._callback.on_comm_log(message)
		self._logger.comm(message)
		self._serialLogger.debug(message)


	def flash_grbl(self, grbl_file=None, verify_only=False, is_connected=True):
		"""
		Flashes the specified grbl file (.hex). This file must not contain a bootloader.
		:param grbl_file:
		:param verify_only: If true, nothing is written, current grbl is verified only
		:param is_connected: If True, serial connection to grbl is closed before flashing and reconnected afterwards.
			Auto updates is executed before connection to grbl is established so in this case this param should be set to False.
		"""
		log_verb = 'verifying' if verify_only else 'flashing'

		if self._state in (self.STATE_FLASHING, self.STATE_PRINTING, self.STATE_PAUSED):
			msg = "{} GRBL not possible in current printer state.".format(log_verb.capitalize())
			self._logger.warn(msg, terminal_as_comm=True)
			return

		if grbl_file is None:
			if self._grbl_version == self.GRBL_VERSION_20170919_22270fa: # legacy version string
				grbl_file = 'grbl_0.9g_20170919_22270fa.hex'
			elif self._grbl_version is not None:
				# '0.9g_20180223_61638c5' => 'grbl_0.9g_20180223_61638c5.hex'
				grbl_file = 'grbl_{}.hex'.format(self._grbl_version)


		if grbl_file is None:
			msg = "ERROR {} GRBL: No default filename for currently installed version '%s'.".format(log_verb, self._grbl_version)
			self._logger.warn(msg, terminal_as_comm=True)
			return

		if grbl_file.startswith('..') or grbl_file.startswith('/'):
			msg = "ERROR {} GRBL '{}': Invalid filename.".format(log_verb, grbl_file)
			self._logger.warn(msg, terminal_as_comm=True)
			return

		from_version = self._grbl_version

		grbl_path = os.path.join(__package_path__, self.GRBL_HEX_FOLDER, grbl_file)
		if not os.path.isfile(grbl_path):
			msg = "ERROR {} GRBL '{}': File not found".format(log_verb, grbl_file)
			self._logger.warn(msg, terminal_as_comm=True)
			return

		self._logger.info("{} grbl: '%s'", log_verb.capitalize(), grbl_path)

		if is_connected:
			self.close(isError=False, next_state=self.STATE_FLASHING)
			time.sleep(1)

		# FYI: Fuses can't be changed from over srial
		params = ["avrdude", "-patmega328p", "-carduino",
		          "-b{}".format(self._baudrate), "-P{}".format(self._port),
		          '-u', '-q', # non inter-active and quiet
		          "-Uflash:{}:{}:i".format('v' if verify_only else 'w',grbl_path)]
		self._logger.debug("flash_grbl() avrdude command:  %s", ' '.join(params))
		output, code = exec_cmd_output(params)

		if output is not None:
			output = output.replace('strace: |autoreset: Broken pipe\n', '')
			output = output.replace('done with autoreset\n', '')

		if not verify_only:
			try:
				_mrbeam_plugin_implementation._analytics_handler.write_flash_grbl(
					from_version=from_version,
					to_version=grbl_file,
					succesful=(code == 0))
			except:
				self._logger.exception("Exception while writing GRBL-flashing to analytics: ")

		# error case
		if code != 0 and not verify_only:
			msg_short = "ERROR flashing GRBL '{}'".format(grbl_file)
			msg_long = '{}:\n{}'.format(msg_short, output)
			self._logger.error(msg_long, terminal_as_comm=True)
			self._logger.error(msg_short, terminal_as_comm=True)
			self._errorValue = "avrdude returncode: %s" % code
			self._changeState(self.STATE_CLOSED_WITH_ERROR)
			self._logger.info("Please reconnect manually or reboot system.", terminal_as_comm=True)
			return
		elif code != 0 and verify_only:
			msg_short = "Verification GRBL '{}': FAILED (See Avrdude output above for details.)".format(grbl_file)
			msg_long = '{}:\n{}'.format(msg_short, output)
			self._logger.info(msg_long, terminal_as_comm=True)
			self._logger.info(msg_short, terminal_as_comm=True)
		elif code == 0 and verify_only:
			msg_short = "Verification GRBL '{}': OK".format(grbl_file)
			msg_long = '{}:\n{}'.format(msg_short, output)
			self._logger.info(msg_long, terminal_as_comm=True)
			self._logger.info(msg_short, terminal_as_comm=True)
		elif code == 0 and not verify_only:
			# ok case
			msg_short = "OK flashing GRBL '{}'".format(grbl_file)
			msg_long = '{}:\n{}'.format(msg_short, output)
			self._logger.debug(msg_long, terminal_as_comm=True)
			self._logger.info(msg_short, terminal_as_comm=True)

		time.sleep(1.0)

		# reconnect
		if is_connected:
			timeout = 60
			self._logger.info("Waiting before reconnect. (max %s secs)", timeout, terminal_as_comm=True)
			if self.monitoring_thread is not None and threading.current_thread() != self.monitoring_thread:
				self.monitoring_thread.join(timeout)

			if self.monitoring_thread is not None and not self.monitoring_thread.isAlive():
				# will open serial connection
				self._start_monitoring_thread()
			else:
				self._logger.info("Can't reconnect automacically. Try to reconnect manually or reboot system.")


	def reset_grbl_auto_update_config(self):
		"""
		Resets grbl auto update configuration in lasercutterProfile if current grbl version is expected version.
		This makes sure that once the auto update got executed sucessfully it's not done again and again.
		Only has effect IF:
		 - grbl_auto_update_enabled in config.yaml is True (default)
		"""
		if self.grbl_auto_update_enabled and self._laserCutterProfile['grbl']['auto_update_version'] is not None:
			if self._grbl_version == self._laserCutterProfile['grbl']['auto_update_version']:
				self._logger.info("Removing grbl auto update flags from lasercutterprofile...")
				try:
					self._laserCutterProfile['grbl']['auto_update_file'] = None
					self._laserCutterProfile['grbl']['auto_update_version'] = None
					laserCutterProfileManager().save(self._laserCutterProfile, allow_overwrite=True)
				except:
					self._logger.exception("Exception while saving lasercutterProfile changes to auto update controls: ")
			else:
				self._logger.warn("GRBL auto update still set: auto_update_file: %s, auto_update_version: %s, current grbl version: %s",
				                  self._laserCutterProfile['grbl']['auto_update_file'],
				                  self._laserCutterProfile['grbl']['auto_update_version'],
				                  self._grbl_version)


	def rescue_from_home_pos(self):
		"""
		In case the laserhead is pushed deep into homing corner and constantly keeps endstops/limit switches pushed,
		this is going to rescue it from there before homing cycle is started.

		This method tests:
		- If GRBL version supports rescue (means reports limit data)
		- If laserhead needs to be rescued
		And then rescues aka moves the laserhead out of the critical zone.

		Requires GRBL v '0.9g_20180223_61638c5' because we need limit data reported.
		"""
		if self._grbl_version is None:
			self._logger.warn("rescue_from_home_pos() No GRBL version yet.")
			return

		if not self.grbl_feat_rescue_from_home:
			self._logger.info("rescue_from_home_pos() Rescue from home not supported by current GRBL version. GRBL version: %s", self._grbl_version)
			return
		else:
			self._logger.info("rescue_from_home_pos() GRBL version: %s", self._grbl_version)


		if self.limit_x < 0 or self.limit_y < 0:
			self._logger.debug("rescue_from_home_pos() No limit data yet. Requesting status update from GRBL...")
			self.sendCommand(self.COMMAND_STATUS)
			i=0
			while i<200 and (self.limit_x < 0 or self.limit_y < 0):
				i += 1
				self._logger.debug("rescue_from_home_pos() sleeping... (%s)", i)
				time.sleep(0.01)

		if self.limit_x < 0 or self.limit_y < 0:
			self._logger.warn("rescue_from_home_pos() Can't get status with limit data. Returning.")
			return

		if self.limit_x == 0 and self.limit_y == 0:
			self._logger.debug("rescue_from_home_pos() Not in home pos. nothing to rescue.")
			return

		self._logger.info("rescue_from_home_pos() Rescuing laserhead from home position...")
		self.sendCommand('$X')
		self.sendCommand(self.COMMAND_FLUSH)
		self.sendCommand('G91')
		self.sendCommand('G1X{x}Y{y}F500S0'.format(x='-5' if self.limit_x > 0 else '0', y='-5' if self.limit_y > 0 else '0'))
		self.sendCommand('G90')
		self.sendCommand(self.COMMAND_FLUSH)
		time.sleep(1) # turns out we need this :-/ Maybe SYNC will solve once SYNC is fully working


	def _handle_command_handler_result(self, command, command_type, gcode, handler_result):
		original_tuple = (command, command_type, gcode)

		if handler_result is None:
			# handler didn't return anything, we'll just continue
			return original_tuple

		if isinstance(handler_result, basestring):
			# handler did return just a string, we'll turn that into a 1-tuple now
			handler_result = (handler_result,)
		elif not isinstance(handler_result, (tuple, list)):
			# handler didn't return an expected result format, we'll just ignore it and continue
			return original_tuple

		hook_result_length = len(handler_result)
		if hook_result_length == 1:
			# handler returned just the command
			command, = handler_result
		elif hook_result_length == 2:
			# handler returned command and command_type
			command, command_type = handler_result
		else:
			# handler returned a tuple of an unexpected length
			return original_tuple

		gcode = self._gcode_command_for_cmd(command)
		return command, command_type, gcode

	def _set_feedrate_override(self, value):
		temp = value / 100.0
		if temp > 0:
			self._feedrate_factor = temp
			self._feedrate_dict = {}
			if self._actual_feedrate is not None:
				temp = round(self._actual_feedrate * self._feedrate_factor)
				# TODO replace with value from printer profile
				if temp > 5000:
					temp = 5000
				elif temp < 30:
					temp = 30
				self.sendCommand('F%d' % round(temp))

	def _set_intensity_override(self, value):
		temp = value / 100.0
		if temp >= 0:
			self._intensity_factor = temp
			self._intensity_dict = {}
			if self._actual_intensity is not None:
				intensity_limit = int(self._laserCutterProfile['laser']['intensity_limit'])
				temp = round(self._actual_intensity * self._intensity_factor)
				if temp > intensity_limit:
					temp = intensity_limit
				self.sendCommand('S%d' % round(temp))

	def _replace_feedrate(self, cmd):
		obj = self._regex_feedrate.search(cmd)
		if obj is not None:
			feedrate_cmd = cmd[obj.start():obj.end()]
			self._actual_feedrate = int(feedrate_cmd[1:])
			if self._feedrate_factor != 1:
				if feedrate_cmd in self._feedrate_dict:
					new_feedrate = self._feedrate_dict[feedrate_cmd]
				else:
					new_feedrate = round(self._actual_feedrate * self._feedrate_factor)
					# TODO replace with value from printer profile
					if new_feedrate > 5000:
						new_feedrate = 5000
					elif new_feedrate < 30:
						new_feedrate = 30
					self._feedrate_dict[feedrate_cmd] = new_feedrate
				return cmd.replace(feedrate_cmd, 'F%d' % round(new_feedrate))
		return cmd

	def _replace_intensity(self, cmd):
		obj = self._regex_intensity.search(cmd)
		if obj is not None:
			intensity_limit = int(self._laserCutterProfile['laser']['intensity_limit'])
			intensity_cmd = cmd[obj.start():obj.end()]
			parsed_intensity = int(intensity_cmd[1:])
			self._actual_intensity = parsed_intensity if parsed_intensity <= intensity_limit else intensity_limit
			if self._actual_intensity != parsed_intensity:
				return cmd.replace(intensity_cmd, 'S%d' % round(self._actual_intensity))
			elif self._intensity_factor != 1:
				# _intensity_factor is deprecated
				if intensity_cmd in self._intensity_dict:
					new_intensity = self._intensity_dict[intensity_cmd]
				else:
					new_intensity = round(self._actual_intensity * self._intensity_factor)
					if new_intensity > intensity_limit:
						new_intensity = intensity_limit
					self._intensity_dict[intensity_cmd] = new_intensity
				return cmd.replace(intensity_cmd, 'S%d' % round(new_intensity))
		return cmd

	def get_hex_str_from_str(self, data):
		res = []
		for i in range(len(data)):
			tmp = hex(ord(data[i]))[2:].upper()
			res.append('0'+tmp if len(tmp)<=1 else tmp)
		return ' '.join(res)

	##~~ command handlers
	def _gcode_G1_sending(self, cmd, cmd_type=None):
		cmd = self._replace_feedrate(cmd)
		cmd = self._replace_intensity(cmd)
		return cmd

	def _gcode_G2_sending(self, cmd, cmd_type=None):
		cmd = self._replace_feedrate(cmd)
		cmd = self._replace_intensity(cmd)
		return cmd

	def _gcode_G3_sending(self, cmd, cmd_type=None):
		cmd = self._replace_feedrate(cmd)
		cmd = self._replace_intensity(cmd)
		return cmd

	def _gcode_M3_sending(self, cmd, cmd_type=None):
		cmd = self._replace_feedrate(cmd)
		cmd = self._replace_intensity(cmd)
		return cmd

	def _gcode_G01_sending(self, cmd, cmd_type=None):
		return self._gcode_G1_sending(cmd, cmd_type)

	def _gcode_G02_sending(self, cmd, cmd_type=None):
		return self._gcode_G2_sending(cmd, cmd_type)

	def _gcode_G03_sending(self, cmd, cmd_type=None):
		return self._gcode_G3_sending(cmd, cmd_type)

	def _gcode_M03_sending(self, cmd, cmd_type=None):
		return self._gcode_M3_sending(cmd, cmd_type)

	def _gcode_X_sent(self, cmd, cmd_type=None):
		# since we use $X to rescue from homeposition, we don't want this to trigger homing
		# self._changeState(self.STATE_HOMING)  # TODO: maybe change to seperate $X mode
		return cmd

	def _gcode_H_sent(self, cmd, cmd_type=None):
		self._changeState(self.STATE_HOMING)
		return cmd

	def _gcode_Hold_sent(self, cmd, cmd_type=None):
		self._changeState(self.STATE_PAUSED)
		return cmd

	def _gcode_Resume_sent(self, cmd, cmd_type=None):
		self._changeState(self.STATE_PRINTING)
		return cmd

	def _gcode_F_sending(self, cmd, cmd_type=None):
		cmd = self._replace_feedrate(cmd)

	def _gcode_S_sending(self, cmd, cmd_type=None):
		cmd = self._replace_intensity(cmd)

	def sendCommand(self, cmd, cmd_type=None, processed=False):
		if cmd is not None and cmd.strip().startswith('/'):
			try:
				cmd = cmd.strip()
				self._log("Command: %s" % cmd)
				self._logger.info("Terminal user command: %s", cmd)
				tokens = cmd.split(' ')
				specialcmd = tokens[0].lower()
				if specialcmd.startswith('/togglestatusreport'):
					if self._status_polling_interval <= 0:
						self._set_status_polling_interval_for_state()
					else:
						self._status_polling_interval = 0
				elif specialcmd.startswith('/setstatusfrequency'):
					try:
						self._status_polling_interval = float(tokens[1])
					except ValueError:
						self._log("No frequency setting found! No change")
				elif specialcmd.startswith('/disconnect'):
					self.close()
				elif specialcmd.startswith('/feedrate'):
					if len(tokens) > 1:
						self._set_feedrate_override(int(tokens[1]))
					else:
						self._log("no feedrate given")
				elif specialcmd.startswith('/intensity'):
					if len(tokens) > 1:
						data = specialcmd[8:]
						self._set_intensity_override(int(tokens[1]))
					else:
						self._log("no intensity given")
				elif specialcmd.startswith('/reset'):
					self._log("Reset initiated")
					self._serial.write(list(bytearray('\x18')))
				elif specialcmd.startswith('/flash_grbl'):
					file = None
					if len(tokens) > 1:
						file = tokens[1]
						self._log("Flashing GRBL '%s'..." % file)
					else:
						self._log("Flashing GRBL...")
					self.flash_grbl(file)
				elif specialcmd.startswith('/verify_grbl'):
					file = None
					if len(tokens) > 1:
						file = tokens[1]
						self._log("Verifying GRBL '%s'..." % file)
					else:
						self._log("Verifying GRBL...")
					self.flash_grbl(file, verify_only=True)
				elif specialcmd.startswith('/correct_settings'):
					self._log("Correcting GRBL settings...")
					self.correct_grbl_settings()
				else:
					self._log("Command not found.")
					self._log("Available commands are:")
					self._log("   /togglestatusreport")
					self._log("   /setstatusfrequency <interval secs>")
					self._log("   /feedrate <f>")
					self._log("   /intensity <s>")
					self._log("   /disconnect")
					self._log("   /reset")
					self._log("   /correct_settings")
					self._log("   /verify_grbl [<file>]")
					self._log("   /flash_grbl [<file>]")
			except:
				self._logger.exception("Exception while executing terminal command '%s'", cmd, terminal_as_comm=True)
		else:
			cmd = cmd.encode('ascii', 'replace')
			if not processed:
				cmd = process_gcode_line(cmd)
				if not cmd:
					return

			eepromCmd = re.search("^\$[0-9]+=.+$", cmd)
			if(eepromCmd and self.isPrinting()):
				self._log("Warning: Configuration changes during print are not allowed!")

			self._commandQueue.put(cmd)
			self._send_event.set()

	def selectFile(self, filename, sd):
		if self.isBusy():
			return

		self._currentFile = PrintingGcodeFileInformation(filename)
		eventManager().fire(OctoPrintEvents.FILE_SELECTED, {
			"file": self._currentFile.getFilename(),
			"filename": os.path.basename(self._currentFile.getFilename()),
			"origin": self._currentFile.getFileLocation()
		})
		self._callback.on_comm_file_selected(filename, self._currentFile.getFilesize(), False)

	def unselectFile(self):
		if self.isBusy():
			return

		self._currentFile = None
		eventManager().fire(OctoPrintEvents.FILE_DESELECTED)
		self._callback.on_comm_file_selected(None, None, False)

	def startPrint(self, *args, **kwargs):
		# TODO implement pos kw argument for resuming prints
		if not self.isOperational():
			return

		if self._currentFile is None:
			raise ValueError("No file selected for printing")

		# reset feedrate and intesity factor in case they where changed in a previous run
		self._feedrate_factor  = 1
		self._intensity_factor = 1
		self._finished_passes = 0
		self._pauseWaitTimeLost = 0.0
		self._pauseWaitStartTime = None

		self._rx_stats.reset()
		if not self.grbl_feat_report_rx_buffer_state:
			self._rx_stats.set_no_grbl_support()

		try:
			# ensure fan is on whatever gcode follows.
			self.sendCommand("M08")

			self._currentFile.start()
			self._finished_currentFile = False

			payload = {
				"file": self._currentFile.getFilename(),
				"filename": os.path.basename(self._currentFile.getFilename()),
				"origin": self._currentFile.getFileLocation(),
				'mrb_state': _mrbeam_plugin_implementation.get_mrb_state()
			}
			eventManager().fire(OctoPrintEvents.PRINT_STARTED, payload)

			self._changeState(self.STATE_PRINTING)
		except:
			self._logger.exception("Error while trying to start printing")
			self._errorValue = get_exception_string()
			self._changeState(self.STATE_ERROR)
			eventManager().fire(OctoPrintEvents.ERROR, {"error": self.getErrorString()})
			self._logger.dump_terminal_buffer(level=logging.ERROR)

	def cancelPrint(self):
		if not self.isOperational():
			return

		# first pause (feed hold) bevore doing the soft reset in order to retain machine pos.
		self._sendCommand(self.COMMAND_HOLD)
		time.sleep(0.5)

		with self._commandQueue.mutex:
			self._commandQueue.queue.clear()
		self._cmd = None

		self._sendCommand(self.COMMAND_RESET)
		self._acc_line_buffer = []
		self._send_event.clear(completely=True)
		self._changeState(self.STATE_LOCKED)

		payload = {
			"file": self._currentFile.getFilename(),
			"filename": os.path.basename(self._currentFile.getFilename()),
			"origin": self._currentFile.getFileLocation(),
			"time": self.getPrintTime(),
			'mrb_state': _mrbeam_plugin_implementation.get_mrb_state()
		}
		eventManager().fire(OctoPrintEvents.PRINT_CANCELLED, payload)

		self._logger.info(self._rx_stats.pp(print_time=self.getPrintTime()))

	def setPause(self, pause, send_cmd=True, pause_for_cooling=False, trigger=None, force=False):
		if not self._currentFile:
			return

		payload = {
			"file": self._currentFile.getFilename(),
			"filename": os.path.basename(self._currentFile.getFilename()),
			"origin": self._currentFile.getFileLocation(),
			"trigger": trigger,
			"time": self.getPrintTime(),
			'mrb_state': _mrbeam_plugin_implementation.get_mrb_state()
		}

		if not pause and (self.isPaused() or force):
			if self._pauseWaitStartTime:
				self._pauseWaitTimeLost = self._pauseWaitTimeLost + (time.time() - self._pauseWaitStartTime)
				self._pauseWaitStartTime = None
			self._pause_delay_time = time.time()
			payload["time"] = self.getPrintTime() # we need the pasue time to be removed from time
			if send_cmd is True:
				self._real_time_commands['cycle_start']=True
			self._send_event.set()
			eventManager().fire(OctoPrintEvents.PRINT_RESUMED, payload)
		elif pause and (self.isPrinting() or force):
			if not self._pauseWaitStartTime:
				self._pauseWaitStartTime = time.time()
			self._pause_delay_time = time.time()
			if send_cmd is True:
				self._real_time_commands['feed_hold']=True
			self._send_event.set()
			eventManager().fire(OctoPrintEvents.PRINT_PAUSED, payload)
			self._logger.info(self._rx_stats.pp(print_time=self.getPrintTime()))

		if self.getPrintTime() < 0.0:
			self._logger.warn("setPause() %s: print time is negative! print time: %s, _pauseWaitStartTime: %s, _pauseWaitTimeLost: %s",
			                  'pausing' if pause else 'resuming',
			                  self.getPrintTime(),
			                  self._pauseWaitStartTime,
			                  self._pauseWaitTimeLost)

	def increasePasses(self):
		self._passes += 1
		self._log("increased Passes to %d" % self._passes)

	def decreasePasses(self):
		self._passes -= 1
		self._log("decrease Passes to %d" % self._passes)

	def setPasses(self, value):
		self._passes = value
		self._log("set Passes to %d" % self._passes)

	def sendGcodeScript(self, scriptName, replacements=None):
		pass

	def getStateId(self, state=None):
		if state is None:
			state = self._state

		possible_states = filter(lambda x: x.startswith("STATE_"), self.__class__.__dict__.keys())
		for possible_state in possible_states:
			if getattr(self, possible_state) == state:
				return possible_state[len("STATE_"):]

		return "UNKNOWN"

	def getStateString(self, state=None):
		if state is None:
			state = self._state
		if state == self.STATE_NONE:
			return "Offline"
		if state == self.STATE_OPEN_SERIAL:
			return "Opening serial port"
		if state == self.STATE_DETECT_SERIAL:
			return "Detecting serial port"
		if state == self.STATE_DETECT_BAUDRATE:
			return "Detecting baudrate"
		if state == self.STATE_CONNECTING:
			return "Connecting"
		if state == self.STATE_OPERATIONAL:
			return "Operational"
		if state == self.STATE_PRINTING:
			# return "Printing"
			return "Lasering"
		if state == self.STATE_PAUSED:
			return "Paused"
		if state == self.STATE_CLOSED:
			return "Closed"
		if state == self.STATE_ERROR:
			return "Error: %s" % (self.getErrorString())
		if state == self.STATE_CLOSED_WITH_ERROR:
			return "Error: %s" % (self.getErrorString())
		if state == self.STATE_TRANSFERING_FILE:
			return "Transfering file to SD"
		if self._state == self.STATE_LOCKED:
			return "Locked"
		if self._state == self.STATE_HOMING:
			return "Homing"
		if self._state == self.STATE_FLASHING:
			return "Flashing"
		return "Unknown State (%d)" % (self._state)

	def getPrintProgress(self):
		if self._currentFile is None:
			return None
		return self._currentFile.getProgress()

	def getPrintFilepos(self):
		if self._currentFile is None:
			return None
		return self._currentFile.getFilepos()

	def getCleanedPrintTime(self):
		printTime = self.getPrintTime()
		if printTime is None:
			return None
		return printTime

	def getConnection(self):
		return self._port, self._baudrate

	def isOperational(self):
		return self._state == self.STATE_OPERATIONAL or self._state == self.STATE_PRINTING or self._state == self.STATE_PAUSED

	def isPrinting(self):
		return self._state == self.STATE_PRINTING

	def isPaused(self):
		return self._state == self.STATE_PAUSED

	def isLocked(self):
		return self._state == self.STATE_LOCKED

	def isHoming(self):
		return self._state == self.STATE_HOMING

	def isFlashing(self):
		return self._state == self.STATE_FLASHING

	def isBusy(self):
		return self.isPrinting() or self.isPaused()

	def isError(self):
		return self._state == self.STATE_ERROR or self._state == self.STATE_CLOSED_WITH_ERROR

	def isClosedOrError(self):
		return self._state == self.STATE_ERROR or self._state == self.STATE_CLOSED_WITH_ERROR or self._state == self.STATE_CLOSED

	def isSdReady(self):
		return False

	def isStreaming(self):
		return False

	def getErrorString(self):
		return self._errorValue

	def getPrintTime(self):
		if self._currentFile is None or self._currentFile.getStartTime() is None:
			return None
		else:
			return time.time() - self._currentFile.getStartTime() - self._pauseWaitTimeLost

	def getGrblVersion(self):
		return self._grbl_version

	def close(self, isError=False, next_state=None):
		self._monitoring_active = False
		self._sending_active = False
		self._status_polling_interval = 0

		printing = self.isPrinting() or self.isPaused()
		if self._serial is not None:
			if isError:
				self._changeState(self.STATE_CLOSED_WITH_ERROR)
			elif next_state:
				self._changeState(next_state)
			else:
				self._changeState(self.STATE_CLOSED)
			self._serial.close()
		self._serial = None

		if printing:
			payload = None
			if self._currentFile is not None:
				payload = {
					"file": self._currentFile.getFilename(),
					"filename": os.path.basename(self._currentFile.getFilename()),
					"origin": self._currentFile.getFileLocation(),
					"time": self.getPrintTime(),
					"mrb_state": _mrbeam_plugin_implementation.get_mrb_state()
				}
			eventManager().fire(OctoPrintEvents.PRINT_FAILED, payload)
		eventManager().fire(OctoPrintEvents.DISCONNECTED)

### MachineCom callback ################################################################################################
class MachineComPrintCallback(object):
	def on_comm_log(self, message):
		pass

	def on_comm_temperature_update(self, temp, bedTemp):
		pass

	def on_comm_state_change(self, state):
		pass

	def on_comm_message(self, message):
		pass

	def on_comm_progress(self):
		pass

	def on_comm_print_job_done(self):
		pass

	def on_comm_z_change(self, newZ):
		pass

	def on_comm_file_selected(self, filename, filesize, sd):
		pass

	def on_comm_sd_state_change(self, sdReady):
		pass

	def on_comm_sd_files(self, files):
		pass

	def on_comm_file_transfer_started(self, filename, filesize):
		pass

	def on_comm_file_transfer_done(self, filename):
		pass

	def on_comm_force_disconnect(self):
		pass

	def on_comm_pos_update(self, MPos, WPos):
		pass

class PrintingFileInformation(object):
	"""
	Encapsulates information regarding the current file being printed: file name, current position, total size and
	time the print started.
	Allows to reset the current file position to 0 and to calculate the current progress as a floating point
	value between 0 and 1.
	"""

	def __init__(self, filename):
		self._logger = mrb_logger("octoprint.plugins.mrbeam.comm_acc2")
		self._filename = filename
		self._pos = 0
		self._size = None
		self._comment_size = None
		self._start_time = None

	def getStartTime(self):
		return self._start_time

	def getFilename(self):
		return self._filename

	def getFilesize(self):
		return self._size

	def getFilepos(self):
		return self._pos - self._comment_size

	def getFileLocation(self):
		return FileDestinations.LOCAL

	def getProgress(self):
		"""
		The current progress of the file, calculated as relation between file position and absolute size. Returns -1
		if file size is None or < 1.
		"""
		if self._size is None or not self._size > 0:
			return -1
		return float(self._pos - self._comment_size) / float(self._size - self._comment_size)

	def reset(self):
		"""
		Resets the current file position to 0.
		"""
		self._pos = 0

	def start(self):
		"""
		Marks the print job as started and remembers the start time.
		"""
		self._start_time = time.time()

	def close(self):
		"""
		Closes the print job.
		"""
		pass

class PrintingGcodeFileInformation(PrintingFileInformation):
	"""
	Encapsulates information regarding an ongoing direct print. Takes care of the needed file handle and ensures
	that the file is closed in case of an error.
	"""

	def __init__(self, filename, offsets_callback=None, current_tool_callback=None):
		PrintingFileInformation.__init__(self, filename)

		self._handle = None

		self._first_line = None

		self._offsets_callback = offsets_callback
		self._current_tool_callback = current_tool_callback

		if not os.path.exists(self._filename) or not os.path.isfile(self._filename):
			raise IOError("File %s does not exist" % self._filename)

		self._size = os.stat(self._filename).st_size
		self._pos = 0
		self._comment_size = 0

	def start(self):
		"""
		Opens the file for reading and determines the file size.
		"""
		PrintingFileInformation.start(self)
		self._handle = open(self._filename, "r")

	def close(self):
		"""
		Closes the file if it's still open.
		"""
		PrintingFileInformation.close(self)
		if self._handle is not None:
			try:
				self._handle.close()
			except:
				pass
		self._handle = None

	def resetToBeginning(self):
		"""
		resets the file handle so you can read from the beginning again.
		"""
		self._handle = open(self._filename, "r")

	def getNext(self):
		"""
		Retrieves the next line for printing.
		"""
		if self._handle is None:
			raise ValueError("File %s is not open for reading" % self._filename)

		try:
			processed = None
			while processed is None:
				if self._handle is None:
					# file got closed just now
					return None
				line = self._handle.readline()
				if not line:
					self.close()
				processed = process_gcode_line(line)
				if processed is None:
					self._comment_size += len(line)
			self._pos = self._handle.tell()

			return processed
		except Exception as e:
			self.close()
			self._logger.exception("Exception while processing line")
			raise e


class RxBufferStats(object):

	def __init__(self):
		self.data = {}
		self.count = 0
		self.errs = 0
		self.grbl_support = True
		self.start_ts = time.time()

	def reset(self):
		self.__init__()

	def add(self, val):
		if not self.grbl_support:
			return
		try:
			val = int(val)
		except:
			self.errs += 1
			return
		if not val in self.data:
			self.data[val] = 0
		self.data[val] += 1
		self.count += 1

	def set_no_grbl_support(self):
		self.grbl_support = False

	def get_min(self):
		if self.count <= 0: return 0
		for v, c in sorted(self.data.iteritems()):
			if c > 0:
				return v

	def get_max(self):
		if self.count <= 0: return 0
		for v, c in sorted(self.data.iteritems(), reverse = True):
			if c > 0:
				return v

	def get_avg(self):
		if self.count <= 0: return 0
		avg = 0
		for v, c in sorted(self.data.iteritems()):
			if c > 0:
				avg += v * c
		return avg / self.count

	def pp(self, print_time=-1):
		if not self.grbl_support:
			return "RxBufferStats: not supported by grbl."

		if print_time <=0:
			print_time = time.time() - self.start_ts

		msg = "RxBufferStats: {count} responses within {duration_h} ({duration:.2f}s): {resp_per_sec:.1f} resp/s; invalid: {errs}; min: {min}, max: {max}, avg: {avg}  - All data: ".format(
			count=self.count,
			duration=print_time,
			duration_h=datetime.datetime.fromtimestamp(print_time).strftime('%H°%M′%S″'),
			resp_per_sec=self.count/print_time,
			errs=self.errs,
			min=self.get_min(),
			max=self.get_max(),
			avg=self.get_avg()
		)

		d = []
		for v, c in sorted(self.data.iteritems()):
			if c > 0:
				d.append("{v}:{c}".format(v=v, c=c))
		msg += ', '.join(d)

		return msg





def convert_pause_triggers(configured_triggers):
	triggers = {
		"enable": [],
		"disable": [],
		"toggle": []
	}
	for trigger in configured_triggers:
		if not "regex" in trigger or not "type" in trigger:
			continue

		try:
			regex = trigger["regex"]
			t = trigger["type"]
			if t in triggers:
				# make sure regex is valid
				re.compile(regex)
				# add to type list
				triggers[t].append(regex)
		except re.error:
			# invalid regex or something like this, we'll just skip this entry
			pass

	result = dict()
	for t in triggers.keys():
		if len(triggers[t]) > 0:
			result[t] = re.compile("|".join(map(lambda pattern: "({pattern})".format(pattern=pattern), triggers[t])))
	return result


def process_gcode_line(line):
	line = strip_comment(line).strip()
	line = line.replace(" ", "")
	if not len(line):
		return None
	return line

def strip_comment(line):
	if not ";" in line:
		# shortcut
		return line

	escaped = False
	result = []
	for c in line:
		if c == ";" and not escaped:
			break
		result += c
		escaped = (c == "\\") and not escaped
	return "".join(result)

def get_new_timeout(t):
	now = time.time()
	return now + get_interval(t)

def get_interval(t):
	if t not in default_settings["serial"]["timeout"]:
		return 0
	else:
		return settings().getFloat(["serial", "timeout", t])

def serialList():
	baselist = []
	baselist = baselist \
				+ glob.glob("/dev/ttyUSB*") \
				+ glob.glob("/dev/ttyACM*") \
				+ glob.glob("/dev/ttyAMA*") \
				+ glob.glob("/dev/tty.usb*") \
				+ glob.glob("/dev/cu.*") \
				+ glob.glob("/dev/cuaU*") \
				+ glob.glob("/dev/rfcomm*")

	additionalPorts = settings().get(["serial", "additionalPorts"])
	for additional in additionalPorts:
		baselist += glob.glob(additional)

	prev = settings().get(["serial", "port"])
	if prev in baselist:
		baselist.remove(prev)
		baselist.insert(0, prev)
	if settings().getBoolean(["devel", "virtualPrinter", "enabled"]):
		baselist.append("VIRTUAL")
	return filter(None, baselist)

def baudrateList():
	ret = [250000, 230400, 115200, 57600, 38400, 19200, 9600]
	prev = settings().getInt(["serial", "baudrate"])
	if prev in ret:
		ret.remove(prev)
		ret.insert(0, prev)
	return ret
