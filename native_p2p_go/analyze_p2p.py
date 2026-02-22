import struct

# First capture from logs - p2pSave/3.p2p (368 bytes), type=3
raw = [3, 0, 0, 0, 3, 13, -44, 24, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 49, 0, 112, -128, 112, -128, -96, 41, 103, -24, 0, 0, 0, 0, 52, -55, -119, -50, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 59, 0, 112, -128, 112, -128, -57, 57, 103, -24, 0, 0, 0, 0, 35, 81, -120, 54, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 38, 0, 31, 64, 31, 64, 121, 25, 103, -24, 0, 0, 0, 0]
data = bytes([b & 0xFF for b in raw])

print("Raw hex:", data.hex())
print(f"Total bytes: {len(data)}")
print()

# Expected from logs:
# Server 1: 3.13.212.24:28800 srv_id=49 level=0
# Server 2: 52.201.137.206:28800 srv_id=59 level=0
# Server 3: 35.81.136.54:8000 srv_id=38 level=0

# Header: first 4 bytes
# data[0:4] = 03 00 00 00 -> count=3 (LE) or type=3
count = struct.unpack('<I', data[0:4])[0]
print(f"Header (LE uint32): {count}")
print()

# Now try to identify IP 3.13.212.24 = 0x030DD418
target_ip = bytes([3, 13, 0xD4, 0x18])
pos = data.find(target_ip)
print(f"IP 3.13.212.24 found at offset: {pos}")

# Offset 4: starts the first server entry
# 030DD418 = 3.13.212.24 (the first expected server)
# Then 16 bytes of zeros (IPv6 empty)
# Then remaining fields

# Let's try different entry sizes
for entry_size in [32, 36, 40, 44, 48]:
    print(f"\n=== Entry size = {entry_size}, start at offset 4 ===")
    for i in range(3):
        off = 4 + i * entry_size
        if off + 8 > len(data):
            print(f"  Entry {i}: out of bounds")
            break
        ip = f"{data[off]}.{data[off+1]}.{data[off+2]}.{data[off+3]}"
        print(f"  Entry {i}: IP={ip} raw={data[off:off+min(entry_size, len(data)-off)].hex()}")
