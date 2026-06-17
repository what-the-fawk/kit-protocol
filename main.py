import csv
import argparse
import socket
import sys
import logging

UNIT_CODES = {
    'DC_V': 0b0000000,
    'AC_V': 0b0000001,
}

RANGE_CODES = {
    '400mV': 0b0000000,
    '4V': 0b0000001,
    '40V': 0b0000010,
    '400V': 0b0000011,
    '4000V': 0b0000100,
}

RANGE_DECIMALS = {
    '400mV': 1,
    '4V': 3,
    '40V': 2,
    '400V': 1,
    '4000V': 0
}

RANGE_MAX_VALS = {
    '400mV': 0.400,
    '4V': 4.000,
    '40V': 40.00,
    '400V': 400.0,
    '4000V': 4000.0
}


def create_byte1(sign):
    """
    BYTE 1:
    Bit 0: +/- (0=positive, 1=negative)
    Bit 1: Batt (Hardcoded to 0 = normal)
    Bit 2: OL (Hardcoded to 0 = normal)
    """
    return 1 if sign == '-' else 0


def create_bytes_2_to_5(value_float, range_str):
    """
    BYTE 2-5: ASCII code of measurement value (MSD to LSD).
    Converts a float to a 4-digit zero-padded string based on the range's decimal places.
    """
    decimals = RANGE_DECIMALS[range_str]


    raw_digits = int(round(value_float * (10 ** decimals)))

    if raw_digits > 9999:
        raw_digits = 9999

    digit_str = f"{raw_digits:04d}"

    return [ord(c) for c in digit_str]


def create_protocol_packet(sign, value_float, unit, range_str):
    packet = bytearray(11)

    packet[0] = create_byte1(sign)
    packet[1:5] = create_bytes_2_to_5(value_float, range_str)
    packet[5] = UNIT_CODES[unit]
    packet[6] = RANGE_CODES[range_str]

    packet[7] = 0x00
    packet[8] = 0x00

    packet[9] = 0x0D
    packet[10] = 0x0A

    return packet


def validate_row(row, row_num):
    """Validates CSV row and returns cleaned parameters."""
    errors = []

    sign = row.get('sign', '').strip()
    if sign not in ['+', '-']:
        errors.append(f"Invalid sign '{sign}'. Must be '+' or '-'.")

    try:
        val = float(row.get('value', ''))
        if val < 0:
            errors.append("Value must be non-negative. Use the 'sign' column for polarity.")
    except ValueError:
        errors.append(f"Invalid value '{row.get('value')}'. Must be a valid float.")
        val = None

    unit = row.get('unit', '').strip().upper()
    if unit not in UNIT_CODES:
        errors.append(f"Invalid unit '{unit}'. Supported: {list(UNIT_CODES.keys())}")

    range_str = row.get('range', '').strip()
    if range_str not in RANGE_CODES:
        errors.append(f"Invalid range '{range_str}'. Supported: {list(RANGE_CODES.keys())}")
    else:
        if val is not None and val > RANGE_MAX_VALS[range_str]:
            errors.append(f"Value {val} exceeds maximum for range {range_str} ({RANGE_MAX_VALS[range_str]}).")

    if errors:
        raise ValueError(f"Row {row_num} errors: " + "; ".join(errors))

    return sign, val, unit, range_str


def format_packet(data, as_hex=False):
    if as_hex:
        return ' '.join(f'{byte:02X}' for byte in data)
    else:
        return ' '.join(f'{byte:08b}' for byte in data)


def setup_logging(fallback_file):
    log_filename = fallback_file + '.log'
    root_logger = logging.getLogger()
    if root_logger.handlers:
        root_logger.handlers.clear()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='M9803R Protocol CSV to TCP/Fallback Sender')
    parser.add_argument('csv_file', help='Input CSV file')
    parser.add_argument('--host', help='TCP server IP/hostname (optional)')
    parser.add_argument('--port', type=int, help='TCP server port (optional)')
    parser.add_argument('--fallback-file', required=True, help='File to write raw bytes if TCP fails')
    parser.add_argument('--hex', action='store_true', help='Log packets in hexadecimal format instead of binary')

    args = parser.parse_args()
    logger = setup_logging(args.fallback_file)

    logger.info("=" * 80)
    logger.info("Starting M9803R Protocol Sender")
    logger.info(f"CSV File: {args.csv_file}")
    logger.info(f"Fallback File: {args.fallback_file}")
    logger.info(f"Log Format: {'HEX' if args.hex else 'BINARY'}")

    use_fallback = False
    sock = None
    fallback_f = None

    if not args.host or not args.port:
        use_fallback = True
        error_msg = "Connection error: " + ("Neither host nor port specified" if not args.host and not args.port else (
            "Host not specified" if not args.host else "Port not specified"))
        logger.error(error_msg)
        logger.info("Falling back to file output mode")
    else:
        logger.info(f"Attempting to connect to TCP server {args.host}:{args.port}...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((args.host, args.port))
            logger.info(f"Successfully connected to {args.host}:{args.port}. Sending via TCP.")
        except Exception as e:
            use_fallback = True
            logger.error(f"TCP Connection failed: {e}")
            logger.info(f"Falling back to writing raw bytes to: {args.fallback_file}")

    if use_fallback:
        try:
            fallback_f = open(args.fallback_file, 'wb')
        except Exception as e:
            logger.critical(f"Failed to open fallback file '{args.fallback_file}': {e}")
            sys.exit(1)

    try:
        with open(args.csv_file, 'r', newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)

            required_cols = ['sign', 'value', 'unit', 'range']
            if reader.fieldnames is None or not all(col in reader.fieldnames for col in required_cols):
                logger.error(f"CSV must contain columns: {required_cols}")
                sys.exit(1)

            logger.info("Starting data processing...")
            logger.info("-" * 80)

            success_count = 0
            error_count = 0

            for row_num, row in enumerate(reader, start=1):
                try:
                    sign, val, unit, range_str = validate_row(row, row_num)

                    packet = create_protocol_packet(sign, val, unit, range_str)
                    packet_str = format_packet(packet, as_hex=args.hex)

                    logger.info(f"Row {row_num:02} | {packet_str}")
                    print(f"Row {row_num:02} | {packet_str}")

                    if use_fallback:
                        fallback_f.write(packet)
                        fallback_f.flush()
                    else:
                        try:
                            sock.sendall(packet)
                        except Exception as send_err:
                            logger.error(f"TCP Send failed at Row {row_num}: {send_err}")
                            logger.info(f"Switching to fallback file: {args.fallback_file}")
                            use_fallback = True
                            if fallback_f is None:
                                fallback_f = open(args.fallback_file, 'wb')
                            fallback_f.write(packet)
                            fallback_f.flush()

                    success_count += 1

                except ValueError as e:
                    logger.warning(str(e))
                    error_count += 1
                except Exception as e:
                    logger.error(f"Row {row_num}: Unexpected error - {e}")
                    error_count += 1

            logger.info("-" * 80)
            logger.info(f"Processing complete. Success: {success_count}, Errors: {error_count}")

    except FileNotFoundError:
        logger.error(f"CSV file '{args.csv_file}' not found.")
        sys.exit(1)
    finally:
        if sock:
            sock.close()
        if fallback_f:
            fallback_f.close()
            if use_fallback:
                logger.info(f"Fallback file '{args.fallback_file}' saved successfully (Raw Binary)")
        logger.info("=" * 80)


if __name__ == '__main__':
    main()