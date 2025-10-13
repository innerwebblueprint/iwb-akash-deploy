# IWB Akash Deploy - AI Coding Assistant Instructions

## Project Overview
This Python script is for use inside the IWBDPP (InnerWebBlueprint Digital Pressence Platform) https://github.com/innerwebblueprint/iwb-digital-presence-platform container and intended for use in n8n workflows.

The script orchestrates the complete deployment lifecycle for instances on the Akash decentralized cloud network. It handles wallet management, certificate management, deployment creation, bid selection, lease management, and service monitoring, outputting clean JSON for n8n workflow integration.

The script expects a properly formated Akash SDL passed using the appropriate flag:
-f, --file FILE       Path to Akash SDL YAML file
-y YAML, --yaml YAML  Custom YAML manifest

# IMPORTANT NOTES - DO NOT FORGET

## ⚠️ CRITICAL: When Running Locally Always Source Environment Variables First !

### Environment Variables
**ALWAYS source the test-env-vars.sh file before running commands:**

```bash
source test-env-vars.sh
```

## Running Commands
**CORRECT way to run the script:**
```bash
source test-env-vars.sh && python3 iwb-akash-deploy.py -f test.yml
```

**WRONG ways (DO NOT USE):**
- ❌ `python3 iwb-akash-deploy.py -f test.yml` (without sourcing env vars first)
- ❌ `export COMPOSE_PROJECT_NAME=tdk && python3 ...` (export doesn't persist to Python subprocess)

## Testing Commands
**Check lease status:**
```bash
source test-env-vars.sh
provider-services lease-status --dseq 12645678 --gseq 1 --oseq 1 \
  --provider akashprovideraddress \
  --keyring-backend test --from walletname \
  --node https://akash-rpcnode:443 --auth-type mtls
```


## Recent Fixes (2025-10-11)

### ✅ Fixed: service_url and api_credentials now properly returned to n8n

**Problem**: When script detected an existing deployment, it returned empty `service_url` and `api_credentials` fields, making n8n integration impossible.

**Solution**: 
1. Enhanced `check_service_status()` to extract URIs from lease-status JSON output
2. Added `get_service_url_from_lease()` method to query and construct service URL from URIs
3. Updated `run()` method to:
   - Query lease-status for URIs when existing deployment is found
   - Generate API credentials with actual service URL (not placeholder)
   - Save both to state file for future use
4. Fixed `generate_api_credentials()` to accept service_url parameter

**Result**: Script now returns complete information for n8n:
```json
{
  "service_url": "https://provided-url",
  "api_credentials": {
    "username": "xxxx",
    "password": "xxxx",
    "api_url": "https://provided-url"
  }
}
```
