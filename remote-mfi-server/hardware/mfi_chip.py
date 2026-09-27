"""MFi authentication coprocessor register client over I2C.

Port of shared/.../mfi/MfiAuthenticationClient.kt (the register sequence is
unchanged; only the transport is CH341-over-pyusb instead of Android USB Host).

Registers:
  0x02 protocol major (1 byte)
  0x05 error code (1 byte, best effort)
  0x10 auth control/status: write 0x01 = start, read 0x10 = success
  0x11 signature length (2 bytes, big endian)
  0x12 signature data
  0x20 challenge length (2 bytes, big endian)
  0x21 challenge data
  0x30 certificate length (2 bytes, big endian)
  0x31..0x31+n certificate data (128-byte windows)
"""

import time

from ch341 import Ch341I2c

PROTOCOL_MAJOR_REGISTER = 0x02
ERROR_REGISTER = 0x05
AUTH_CONTROL_STATUS_REGISTER = 0x10
RESPONSE_LENGTH_REGISTER = 0x11
RESPONSE_DATA_REGISTER = 0x12
CHALLENGE_LENGTH_REGISTER = 0x20
CHALLENGE_DATA_REGISTER = 0x21
CERTIFICATE_LENGTH_REGISTER = 0x30
CERTIFICATE_DATA_REGISTER = 0x31
CERTIFICATE_REGISTER_WINDOW_BYTES = 128

AUTH_START_COMMAND = 0x01
AUTH_SUCCESS_STATUS = 0x10

INITIAL_AUTH_DELAY_MILLIS = 10
AUTH_POLL_MILLIS = 10
AUTH_TIMEOUT_MILLIS = 3000

DEFAULT_ADDRESS_7BIT = 0x11


class MfiChipError(RuntimeError):
    pass


class MfiChip:
    def __init__(self, transport: Ch341I2c, address7: int = DEFAULT_ADDRESS_7BIT) -> None:
        self.transport = transport
        self.address7 = address7
        self._certificate: bytes | None = None
        self._protocol_major: int | None = None

    # -- certificate --------------------------------------------------------

    def protocol_major(self) -> int:
        if self._protocol_major is None:
            self._protocol_major = self._read_register(PROTOCOL_MAJOR_REGISTER, 1)[0]
        return self._protocol_major

    def certificate(self) -> bytes:
        if self._certificate is not None:
            return self._certificate
        length = int.from_bytes(self._read_register(CERTIFICATE_LENGTH_REGISTER, 2), "big")
        if not 1 <= length <= 0xFFFF:
            raise MfiChipError(f"invalid certificate length {length}")
        certificate = bytearray()
        register = CERTIFICATE_DATA_REGISTER
        while len(certificate) < length:
            count = min(CERTIFICATE_REGISTER_WINDOW_BYTES, length - len(certificate))
            certificate.extend(self._read_register(register, count))
            register += 1
        self._certificate = bytes(certificate)
        return self._certificate

    # -- signing ------------------------------------------------------------

    def sign_challenge(self, challenge: bytes) -> bytes:
        if not 1 <= len(challenge) <= 128:
            raise MfiChipError("challenge must be 1..128 bytes")
        self._write_register(CHALLENGE_LENGTH_REGISTER, len(challenge).to_bytes(2, "big"))
        self._write_register(CHALLENGE_DATA_REGISTER, challenge)
        self._write_register(AUTH_CONTROL_STATUS_REGISTER, bytes([AUTH_START_COMMAND]))

        time.sleep(INITIAL_AUTH_DELAY_MILLIS / 1000.0)
        deadline = time.monotonic() + AUTH_TIMEOUT_MILLIS / 1000.0
        while True:
            try:
                if self._read_register(AUTH_CONTROL_STATUS_REGISTER, 1)[0] == AUTH_SUCCESS_STATUS:
                    break
            except Exception:
                pass  # the chip can NAK a poll while it is computing
            if time.monotonic() >= deadline:
                raise MfiChipError(f"MFi authentication timed out (error {self._best_effort_error()})")
            time.sleep(AUTH_POLL_MILLIS / 1000.0)

        length = int.from_bytes(self._read_register(RESPONSE_LENGTH_REGISTER, 2), "big")
        if not 1 <= length <= 0xFFFF:
            raise MfiChipError(f"invalid signature length {length}")
        return self._read_register(RESPONSE_DATA_REGISTER, length)

    # -- helpers ------------------------------------------------------------

    def reset_session(self) -> None:
        """Forgets cached values so the next call re-reads the chip."""
        self._certificate = None
        self._protocol_major = None

    def _read_register(self, register: int, length: int) -> bytes:
        # Register select (write-only) followed by a separate read transaction, matching
        # MfiAuthenticationClient.readRegister().
        self.transport.transaction(self.address7, bytes([register]), 0)
        return self.transport.transaction(self.address7, b"", length)

    def _write_register(self, register: int, data: bytes) -> None:
        self.transport.transaction(self.address7, bytes([register]) + data, 0)

    def _best_effort_error(self) -> str:
        try:
            value = self._read_register(ERROR_REGISTER, 1)[0]
            return f"0x{value:02x}"
        except Exception:
            return "unknown"
