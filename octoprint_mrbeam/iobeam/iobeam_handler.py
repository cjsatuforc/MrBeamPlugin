import logging
import socket
import threading
import time

# singleton
_instance = None

def ioBeamHandler(eventBusOct, socket_file=None):
	global _instance
	if _instance is None:
		_instance = IoBeamHandler(eventBusOct, socket_file)
	return _instance


class IoBeamEvents(object):
	CONNECT =            "iobeam.connect"
	DISCONNECT =         "iobeam.disconnect"
	ONEBUTTON_PRESSED =  "iobeam.onebutton.pressed"
	ONEBUTTON_DOWN =     "iobeam.onebutton.down"
	ONEBUTTON_RELEASED = "iobeam.onebutton.released"
	INTERLOCK_OPEN =     "iobeam.interlock.open"
	INTERLOCK_CLOSED =   "iobeam.interlock.closed"
	LID_OPENED =         "iobeam.lid.opened"
	LID_CLOSED =         "iobeam.lid.closed"


class IoBeamHandler(object):

	# > onebtn:pr
	# > onebtn:dn:< time >
	# > onebtn:rl:< time >
	# > onebtn:error	?
	# > lid:pr
	# > lid:rl:< time >
	# > intlk:op:0
	# > intlk:cl:0
	# > intlk:op:1
	# > intlk:cl:1
	# > intlk:op:2
	# > intlk:cl:2
	# > intlk:op:3
	# > intlk:cl:3
	# > steprun:en
	# > steprun:di

	# < fan:on:< value0 - 100 >
	# > fan:on:ok
	# < fan:off
	# > fan:off:ok
	# < fan:auto
	# > fan:auto:ok
	# < fan:factor:< factor >
	# > fan:factor:ok
	# < fan:version
	# > fan:version:<version-string>
	# < fan:dust
	# > fan:dust:<dust value 0.3>
	# < fan:rpm
	# > fan:rpm:<rpm value>

	# < laser:temp
	# > laser:temp:< temperatur >
	# > laser:temp:error:<error type or message>


	# How to test and debug:
	# in config.yaml set
	#      [plugins mrbeam dev debug] to true (suppresses reconnect on socket timeout) and
	#      [plugins mrbeam dev sockets iobeam] to '/tmp/mrbeam_iobeam.sock' to open the socket without sudo pw
	# and then use  "/usr/bin/nc -U -l /tmp/mrbeam_iobeam.sock"


	SOCKET_FILE = "/var/run/mrbeam_iobeam.sock"
	MAX_ERRORS = 3

	MESSAGE_LENGTH_MAX = 1024
	MESSAGE_NEWLINE = "\n"
	MESSAGE_SEPARATOR = ":"
	MESSAGE_ERROR = "error"

	MESSAGE_DEVICE_ONEBUTTON =          "onebtn"
	MESSAGE_DEVICE_LID =   		        "lid"
	MESSAGE_DEVICE_INTERLOCK =          "intlk"
	MESSAGE_DEVICE_STEPRUN =            "steprun"
	MESSAGE_DEVICE_FAN =	            "fan"
	MESSAGE_DEVICE_LASER =	            "laser"

	MESSAGE_ACTION_ONEBUTTON_PRESSED =  "pr"
	MESSAGE_ACTION_ONEBUTTON_DOWN =     "dn"
	MESSAGE_ACTION_ONEBUTTON_RELEASED = "rl"

	MESSAGE_ACTION_INTERLOCK_OPEN =     "op"
	MESSAGE_ACTION_INTERLOCK_CLOSED =   "cl"

	MESSAGE_ACTION_LID_OPENED =         "pr"
	MESSAGE_ACTION_LID_CLOSED =         "rl"


	def __init__(self, event_bus, socket_file=None):
		self._event_bus = event_bus
		self._logger = logging.getLogger("octoprint.plugins.mrbeam.iobeam")

		self._shutdown_signaled = False
		self._isConnected = False
		self._errors = 0

		self._connectionException = None
		self._interlocks = dict()

		self._initWorker(socket_file)

	def isRunning(self):
		return self._worker.is_alive()

	def isConnected(self):
		return self._isConnected

	def shutdown(self):
		global _instance
		_instance = None
		self._logger.debug("shutdown()")
		self._shutdown_signaled = True

	def is_interlock_closed(self):
		return len(self._interlocks.keys()) == 0

	def open_interlocks(self):
		return self._interlocks.keys()

	def _initWorker(self, socket_file=None):
		self._logger.debug("initializing worker thread")

		# this is needed for unit tests
		if socket_file is not None:
			self.SOCKET_FILE = socket_file

		self._worker = threading.Thread(target=self._work)
		self._worker.daemon = True
		self._worker.start()


	def _work(self):
		self._logger.debug("Worker thread starting, connecting to socket: %s %s", self.SOCKET_FILE, ("MRBEAM_DEBUG" if MRBEAM_DEBUG else ""))

		while not self._shutdown_signaled:
			mySocket = None
			try:
				mySocket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
				mySocket.settimeout(3)
				# self._logger.debug("Connecting to socket...")
				mySocket.connect(self.SOCKET_FILE)
			except socket.error as e:
				self._isConnected = False
				if not self._connectionException == str(e):
					self._logger.warn("IoBeamHandler not able to connect to socket %s, reason: %s. I'll keept trying but I won't log further failures.", self.SOCKET_FILE, e)
					self._connectionException = str(e)
				time.sleep(1)
				continue

			self._isConnected = True
			self._errors = 0
			self._connectionException = None
			self._logger.debug("Socket connected")
			self._fireEvent(IoBeamEvents.CONNECT)

			while not self._shutdown_signaled:
				try:

					try:
						data = mySocket.recv(self.MESSAGE_LENGTH_MAX)
					except Exception as e:
						if MRBEAM_DEBUG and e.message == "timed out":
							self._logger.warn("Connection stale but MRBEAM_DEBUG enabled. Continuing....")
							continue
						else:
							self._logger.warn("Exception while sockect.recv(): %s - Resetting connection...", e)
							break

					if not data:
						self._logger.warn("Connection ended from other side. Closing connection...")
						break

					# here we see what's in the data...
					my_errors = self._handleMessages(data)
					if my_errors > 0:
						self._errors += my_errors
						if self._errors >= self.MAX_ERRORS:
							self._logger.warn("Resetting connection... error_count=%s, Resetting connection...", self._errors)
							break
						else:
							self._logger.warn("Received invalid message, error_count=%s", self._errors)

				except:
					self._logger.exception("Exception in socket loop. Not sure what to do, resetting connection...")

			if mySocket is not None:
				self._logger.debug("Closing socket...")
				mySocket.close()

			self._isConnected = False
			self._fireEvent(IoBeamEvents.DISCONNECT)

			if not self._shutdown_signaled:
				self._logger.debug("Sleeping for a sec before reconnecting...")
				time.sleep(1)

		self._logger.debug("Worker thread stopped.")


	# handles incoming data from the socket.
	# @return int: number of invalid messages 0 means all messages were handled correctly
	def _handleMessages(self, data):
		if not data: return 1

		error_count = 0
		message_list = data.split(self.MESSAGE_NEWLINE)
		for message in message_list:
			if not message: continue
			if message == '.': continue # ping

			err = -1
			self._logger.debug("_handleMessages() handling message: %s", message)

			tokens = message.split(self.MESSAGE_SEPARATOR)
			if len(tokens) <=1:
				err = self._handle_invalid_message(message)
			else:
				device = tokens.pop(0)
				if device == self.MESSAGE_DEVICE_ONEBUTTON:
					err = self._handle_onebutton_message(message, tokens)
				elif device == self.MESSAGE_DEVICE_LID:
					err = self._handle_lid_message(message, tokens)
				elif device == self.MESSAGE_DEVICE_INTERLOCK:
					err = self._handle_interlock_message(message, tokens)
				elif device == self.MESSAGE_DEVICE_STEPRUN:
					err = self._handle_steprun_message(message, tokens)
				elif device == self.MESSAGE_DEVICE_FAN:
					err = self._handle_fan_message(message, tokens)
				elif device == self.MESSAGE_DEVICE_LASER:
					err = self._handle_laser_message(message, tokens)
				elif device == self.MESSAGE_ERROR:
					err = self._handle_error_message(message, tokens)
				else:
					err = 1

			if err >= 0:
				error_count += err

		return error_count



	def _handle_invalid_message(self, message):
		self._logger.warn("Received invalid message: '%s'", message)
		return 1


	def _handle_onebutton_message(self, message, token):
		action = token[0] if len(token)>0 else None
		payload = self._as_number(token[1]) if len(token)>1 else None
		self._logger.debug("_handle_onebutton_message() message: %s, action: %s, payload: %s", message, action, payload)

		if action == self.MESSAGE_ACTION_ONEBUTTON_PRESSED:
			self._fireEvent(IoBeamEvents.ONEBUTTON_PRESSED)
		elif action == self.MESSAGE_ACTION_ONEBUTTON_DOWN and payload is not None:
			self._fireEvent(IoBeamEvents.ONEBUTTON_DOWN, payload)
		elif action == self.MESSAGE_ACTION_ONEBUTTON_RELEASED and payload is not None:
			self._fireEvent(IoBeamEvents.ONEBUTTON_RELEASED, payload)
		elif action == self.MESSAGE_ERROR:
			raise Exception("iobeam received OneButton error: %s", message)
		else:
			return self._handle_invalid_message(message)
		return 0


	def _handle_interlock_message(self, message, tokens):
		lock_state = tokens[0] if len(tokens) > 0 else None
		lock_id = tokens[1] if len(tokens) > 1 else None
		before_state = self.open_interlocks()
		self._logger.debug("_handle_interlock_message() message: %s, lock_id: %s, lock_state: %s, before_state: %s", message, lock_id, lock_state, before_state)

		if lock_id is not None and lock_state == self.MESSAGE_ACTION_INTERLOCK_OPEN:
			self._interlocks[lock_id] = True
		elif lock_id is not None and lock_state == self.MESSAGE_ACTION_INTERLOCK_CLOSED:
			self._interlocks.pop(lock_id, None)
		elif self.MESSAGE_ERROR in message:
			raise Exception("iobeam received InterLock error: %s", message)
		else:
			return self._handle_invalid_message(message)

		now_state = self.open_interlocks()
		if now_state != before_state:
			if self.is_interlock_closed():
				self._fireEvent(IoBeamEvents.INTERLOCK_CLOSED)
			else:
				self._fireEvent(IoBeamEvents.INTERLOCK_OPEN, now_state)

		return 0


	def _handle_lid_message(self, message, token):
		action = token[0] if len(token) > 0 else None
		payload = self._as_number(token[1]) if len(token) > 1 else None
		self._logger.debug("_handle_lid_message() message: %s, action: %s, payload: %s", message, action, payload)

		if action == self.MESSAGE_ACTION_LID_OPENED:
			self._fireEvent(IoBeamEvents.LID_OPENED)
		if action == self.MESSAGE_ACTION_LID_CLOSED:
			self._fireEvent(IoBeamEvents.LID_CLOSED)
		else:
			return self._handle_invalid_message(message)

		return 0

	def _handle_steprun_message(self, message, tokens):
		return 0

	def _handle_fan_message(self, message, tokens):
		return 0

	def _handle_laser_message(self, message, tokens):
		return 0

	def _handle_error_message(self, message, token):
		action = token[0] if len(token) > 0 else None
		if action == "reconnect":
			raise Exception("ioBeam requested to reconnect. Now doing so...")
		return 1


	def _fireEvent(self, event, payload=None):
		self._logger.info("_fireEvent() event:%s, payload:%s", event, payload)
		self._event_bus.fire(event, payload)


	def _as_number(self, str):
		if str is None: return None
		try:
			return float(str)
		except:
			return None

