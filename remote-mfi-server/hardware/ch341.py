"""CH341A USB -> I2C transport (pyusb).

Port of shared/.../transport/Ch341I2cTransport.kt + Ch341I2cStreamEncoder.kt.
The CH341 "stream" (EPP) interface is driven purely through bulk transfers:
a configuration stream selects the I2C clock, then every transaction is one or
more 32-byte stream packets.

Wire format used by the reference implementation:
  0xAA ....... stream marker (start of packet)
  0x74 ....... I2C START
  0x75 ....... I2C STOP
  0x80|n ..... write n bytes (n=0 -> 1); bare 0x80 is answered with a status byte
  0xC0|n ..... read n bytes (n=0 -> 1)
  0x00 ....... end of packet (pad to 32 bytes)
"""

import time

try:  # the pure-Python encoder must be importable without pyusb installed
    import usb.core
    import usb.util
except ImportError:  # pragma: no cover
    usb = None

STREAM_START = 0xAA
START = 0x74
WRITE = 0x80
READ = 0xC0
STOP = 0x75
STREAM_END = 0x00
MAX_STREAM_PACKET_BYTES = 32
MAX_READ_BLOCK_BYTES = 32
ACK_BIT = 0x80


class Ch341Error(RuntimeError):
    pass


class Ch341Nack(Ch341Error):
    pass


def _encoded_length(command: int) -> int:
    encoded = command & 0x3F
    return 1 if encoded == 0 else encoded


def _address_byte(address7: int, read: bool) -> int:
    return (address7 << 1) | (1 if read else 0)


def _legacy_transaction(address7: int, write_data: bytes, read_length: int) -> bytes | None:
    """Short form used when the whole transaction fits one 32-byte stream packet.

    Matches Ch341I2cStreamEncoder.legacyTransaction(): the read address setup uses a
    bare 0x80 (answered with a status byte), unlike the batched form in the segmented path.
    """
    if read_length > MAX_READ_BLOCK_BYTES or len(write_data) + 1 > 0x3F:
        return None
    values: list[int] = [STREAM_START]

    def write(address: int, data: bytes) -> None:
        values.extend([WRITE, address])
        for value in data:
            values.extend([WRITE, value])

    if write_data:
        values.append(START)
        write(_address_byte(address7, False), write_data)
    if read_length > 0:
        values.append(START)
        write(_address_byte(address7, True), b"")
        if read_length > 1:
            values.append(READ | (read_length - 1))
        values.append(READ)
    values.extend([STOP, STREAM_END])
    return bytes(values) if len(values) <= MAX_STREAM_PACKET_BYTES else None


def _encode_transaction(address7: int, write_data: bytes, read_length: int) -> list[bytes]:
    """Returns the 32-byte stream packets for one I2C transaction."""
    if not (0 <= address7 <= 0x7F):
        raise Ch341Error("I2C address must be 7-bit")
    if not write_data and read_length <= 0:
        raise Ch341Error("I2C transaction must read or write")
    legacy = _legacy_transaction(address7, write_data, read_length)
    if legacy is not None:
        return [legacy]

    def address_byte(read: bool) -> int:
        return (address7 << 1) | (1 if read else 0)

    result: list[bytes] = []
    segment: list[int] = [STREAM_START]

    def finish_intermediate() -> None:
        nonlocal segment
        segment.append(STREAM_END)
        segment.extend([0] * (MAX_STREAM_PACKET_BYTES - len(segment)))
        result.append(bytes(segment))
        segment = [STREAM_START]

    def command(values: list[int]) -> None:
        nonlocal segment
        if len(segment) + len(values) + 1 > MAX_STREAM_PACKET_BYTES:
            finish_intermediate()
        segment.extend(values)

    def write(address: int, data: bytes) -> None:
        if not data:
            # Address-only write: one batched 0x80|1 suppresses the status byte.
            command([WRITE | 1, address])
            return
        command([WRITE, address])
        for value in data:
            command([WRITE, value])

    def read(length: int) -> None:
        remaining = length
        while remaining > MAX_READ_BLOCK_BYTES:
            command([READ | MAX_READ_BLOCK_BYTES])
            finish_intermediate()
            remaining -= MAX_READ_BLOCK_BYTES
        if remaining > 1:
            command([READ | (remaining - 1)])
        command([READ])

    if write_data:
        command([START])
        write(address_byte(False), write_data)
    if read_length > 0:
        command([START])
        write(address_byte(True), b"")
        read(read_length)
    if len(segment) + 2 > MAX_STREAM_PACKET_BYTES:
        finish_intermediate()
    segment.extend([STOP, STREAM_END])
    result.append(bytes(segment))
    return result


def _instruction_lengths(segment: bytes) -> tuple[int, int]:
    """(bytes put on the wire, bytes read back) for one stream packet."""
    index = 1
    written = 0
    data = 0
    while index < len(segment):
        command = segment[index]
        index += 1
        if command == STREAM_END:
            break
        if command in (START, STOP):
            continue
        if WRITE <= command < 0xC0:
            count = _encoded_length(command)
            if command & 0x3F == 0:  # only bare 0x80 is answered with status bytes
                written += count
            index += count
        elif command >= 0xC0:
            data += _encoded_length(command)
        else:
            raise Ch341Error(f"unexpected CH341 command 0x{command:02x}")
    return written, data


class Ch341I2c:
    """One CH341A device; serializes transactions."""

    def __init__(
        self,
        vendor_id: int = 0x1A86,
        product_id: int = 0x5512,
        speed_command: int = 0x61,  # 0x60=20kHz 0x61=100kHz 0x62=400kHz 0x63=750kHz
        timeout_ms: int = 1000,
        quiet_ms: int = 25,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.quiet_ms = quiet_ms
        self._configuration = bytes([STREAM_START, speed_command, STREAM_END])

        if usb is None:
            raise Ch341Error("pyusb is not installed (pip install pyusb)")
        device = usb.core.find(idVendor=vendor_id, idProduct=product_id)
        if device is None:
            raise Ch341Error(f"CH341 not found (VID 0x{vendor_id:04x} PID 0x{product_id:04x})")
        self.device = device
        self._detach_kernel_driver()
        try:
            self.device.set_configuration()
        except usb.core.USBError:
            pass  # already configured
        configuration = self.device.get_active_configuration()
        interface = configuration[(0, 0)]
        self.interface_number = interface.bInterfaceNumber
        try:
            usb.util.claim_interface(self.device, self.interface_number)
        except usb.core.USBError as error:
            raise Ch341Error(f"could not claim CH341 interface: {error}") from error
        self.ep_out = usb.util.find_descriptor(
            interface,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
        )
        self.ep_in = usb.util.find_descriptor(
            interface,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
        )
        if self.ep_out is None or self.ep_in is None:
            raise Ch341Error("CH341 has no bulk IN/OUT pair")
        # Select the I2C clock (also resets any previous stream state).
        self._bulk_write(self._configuration)
        self._drain()

    # -- public API ---------------------------------------------------------

    def transaction(self, address7: int, write_data: bytes, read_length: int) -> bytes:
        packets = _encode_transaction(address7, write_data, read_length)
        if write_data == b"" and read_length > 0:
            # The 2.0C revision NACKs a read START that follows STOP too quickly.
            time.sleep(0.005)
        response = bytearray()
        for segment in packets:
            self._bulk_write(segment)
            written, data_bytes = _instruction_lengths(segment)
            if written + data_bytes == 0:
                continue
            received = self._read_until_quiet(written + data_bytes, allow_empty=data_bytes == 0)
            if len(received) < data_bytes:
                raise Ch341Error(
                    f"CH341 answered {len(received)} bytes; expected {data_bytes} data bytes"
                )
            data_start = len(received) - data_bytes
            for offset in range(min(data_start, len(received))):
                if received[offset] & ACK_BIT:
                    raise Ch341Nack(f"CH341 reported an I2C NACK on byte {offset}")
            if data_bytes:
                response.extend(received[data_start:])
        if len(response) != read_length:
            raise Ch341Error(f"CH341 returned {len(response)} bytes; expected {read_length}")
        return bytes(response)

    def close(self) -> None:
        try:
            usb.util.release_interface(self.device, self.interface_number)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.device)
        except Exception:
            pass

    # -- internals ----------------------------------------------------------

    def _detach_kernel_driver(self) -> None:
        try:
            if self.device.is_kernel_driver_active(0):
                self.device.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass  # Windows / already detached

    def _bulk_write(self, data: bytes) -> None:
        try:
            self.ep_out.write(data, timeout=self.timeout_ms)
        except usb.core.USBError as error:
            raise Ch341Error(f"CH341 bulk write failed: {error}") from error

    def _drain(self) -> None:
        while True:
            try:
                chunk = self.ep_in.read(self.ep_in.wMaxPacketSize or 32, timeout=5)
            except usb.core.USBError:
                return
            if not chunk:
                return

    def _read_until_quiet(self, maximum: int, allow_empty: bool) -> bytes:
        """Reads one response; stops after a quiet period because a write-only packet
        may be answered with fewer status bytes than bytes written (or not at all)."""
        result = bytearray()
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        timeout_error = getattr(usb.core, "USBTimeoutError", usb.core.USBError)
        while len(result) < maximum and time.monotonic() < deadline:
            try:
                chunk = self.ep_in.read(maximum - len(result), timeout=self.quiet_ms)
            except timeout_error:
                break
            except usb.core.USBError as error:
                raise Ch341Error(f"CH341 bulk read failed: {error}") from error
            if not chunk:
                break
            result.extend(chunk)
        if not allow_empty and not result:
            raise Ch341Error("CH341 returned no response")
        return bytes(result)
