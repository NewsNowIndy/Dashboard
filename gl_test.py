#!/usr/bin/env python3
# test_gl.py - Simple test script
import os
import sys

# Set environment variables
os.environ['GLOBALEAKS_DEBUG'] = '1'
os.environ['USE_ONION'] = '1'
os.environ['GLOBALEAKS_ONION'] = 'http://kmwvnzitkkcd6xygini7q4qexkmiaj7mxzwkzirhvoxn2b4hbuqg5dyd.onion'
os.environ['TOR_SOCKS'] = '127.0.0.1:9050'

print("Environment variables set:")
print(f"USE_ONION: {os.environ.get('USE_ONION')}")
print(f"GLOBALEAKS_ONION: {os.environ.get('GLOBALEAKS_ONION')}")
print(f"GLOBALEAKS_BASE_URL: {os.environ.get('GLOBALEAKS_BASE_URL')}")
print(f"GLOBALEAKS_USERNAME: {os.environ.get('GLOBALEAKS_USERNAME')}")
print()

try:
    print("Testing basic import...")
    from globaleaks_client import TorGlobaLeaksClient
    print("✓ TorGlobaLeaksClient imported successfully")
    
    print("Creating client...")
    base = os.environ.get('GLOBALEAKS_BASE_URL', '').strip('/')
    user = os.environ.get('GLOBALEAKS_USERNAME', '')
    password = os.environ.get('GLOBALEAKS_PASSWORD', '')
    
    print(f"Base URL (before onion override): {base}")
    print(f"Username: {user}")
    
    client = TorGlobaLeaksClient(base, user, password)
    print(f"✓ Client created, effective base: {client.base}")
    
    print("Testing login...")
    session_id = client.login()
    print(f"✓ Login successful, session: {session_id[:8]}...")
    
    print("Testing tips retrieval...")
    tips = client.list_tips()
    print(f"✓ Retrieved {len(tips)} tips")
    
    if tips:
        print("Sample tip keys:", list(tips[0].keys()))
    
except ImportError as e:
    print(f"❌ Import error: {e}")
    print("Make sure you have the TorGlobaLeaksClient in your globaleaks_client.py")
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    print("Full traceback:")
    traceback.print_exc()