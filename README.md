# IWB Akash Deploy

[![Release](https://img.shields.io/github/v/release/innerwebblueprint/iwb-akash-deploy)](https://github.com/innerwebblueprint/iwb-akash-deploy/releases)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Automaticlly Deploy ComfyUI instances to Akash Network.


## Overview
This script is for use inside the IWBDPP (InnerWebBlueprint Digital Pressence Platform) https://innerwebblueprint.com/projects/iwbdpp container and intended for use in n8n workflows.

The script orchestrates the complete deployment lifecycle for instances on the Akash decentralized cloud network. It handles wallet management, certificate management, deployment creation, bid selection, lease management, and service monitoring, outputting clean JSON for n8n workflow integration.

The script expects a properly formated Akash SDL passed using the appropriate flag:

-f, --file FILE       Path to Akash SDL YAML file
-y YAML, --yaml YAML  Custom YAML manifest

## Features

- **Wallet Management**: Secure wallet recovery and management from encrypted storage
- **Certificate Management**: Automated deplyoment certificate
- **Deployment Creation**: SDL validation and deployment submission
- **Bid Selection**: Intelligent provider selection with GPU preferences and blocklists
- **Lease Management**: Automated lease creation and manifest deployment
- **Service Monitoring**: Health checks and service URL extraction
- **API Credentials**: Automatic generation of ComfyUI API credentials
- **State Persistence**: JSON-based state file for deployment tracking
- **n8n Integration**: Structured JSON output for workflow automation

## Requirements

- Python 3.8+
- Akash CLI tools (`provider-services`)
- Akash wallet with sufficient AKT for deployments

## Usage

This script is designed to be called from within n8n using two nodes:

A node to supply an Akash SDL

An execute command node:

```bash
cat > /tmp/deploy.yaml << 'EOF'
{{ $json.yaml }}
EOF
iwb-akash-deploy --yaml-file /tmp/deploy.yaml
```

### Environment Variables

Required environment variables:
These are automatically supplied with IWBDPP
- `COMPOSE_PROJECT_NAME`: Project identifier (e.g., `tdk`)
- `IWB_STORJ_WPOPS_BUCKET`: Storj bucket name
- `IWB_DOMAIN`: Domain name


### Command Line Options

```bash
python3 iwb-akash-deploy.py [OPTIONS]

options:
  -h, --help            show this help message and exit
  --debug               Enable debug mode
  --dry-run             Validate without deploying
  --check-ready         Check if deployment is ready (services + models)
  --close               Close active deployment
  --status              Check lease status
  --logs                View deployment logs
  --shell               Get interactive shell into container
  --rpc-info            Show RPC info
  --cert-query          Query certificate status for wallet or --cert-owner
                        address
  --cert-add            Ensure certificate exists (generate/publish if
                        missing)
  --cert-new            Create and publish a new certificate
  --cert-overwrite      With --cert-new: revoke existing valid cert(s) before
                        publishing new one
  --cert-revoke-serial CERT_REVOKE_SERIAL
                        Revoke a specific certificate serial
  --cert-owner CERT_OWNER
                        Wallet address owner for --cert-query (defaults to
                        restored wallet address)
  -y YAML, --yaml YAML  Custom YAML manifest
  -f YAML_FILE, --yaml-file YAML_FILE
                        Path to YAML file
```

### Certificate Management

```bash
# Query certs for restored wallet
source test-env-vars.sh && python3 iwb-akash-deploy.py --cert-query

# Query certs for specific owner address
source test-env-vars.sh && python3 iwb-akash-deploy.py --cert-query --cert-owner akash1...

# Ensure cert exists (idempotent; creates/publishes only when missing)
source test-env-vars.sh && python3 iwb-akash-deploy.py --cert-add

# Create and publish a brand new cert (fails if valid cert already exists)
source test-env-vars.sh && python3 iwb-akash-deploy.py --cert-new

# Replace existing valid cert(s): revoke existing then publish new
source test-env-vars.sh && python3 iwb-akash-deploy.py --cert-new --cert-overwrite

# Revoke one cert by serial
source test-env-vars.sh && python3 iwb-akash-deploy.py --cert-revoke-serial <serial>
```

## Development

### Version Management

Use the `bump-version.sh` script to bump the version, update CHANGELOG, and create a release:

```bash
# Bump patch version (1.0.0 -> 1.0.1)
./bump-version.sh patch

# Bump minor version (1.0.0 -> 1.1.0)
./bump-version.sh minor

# Bump major version (1.0.0 -> 2.0.0)
./bump-version.sh major
```

The script will:
1. Check for uncommitted changes (prompts to commit if found)
2. Bump version in `iwb-akash-deploy.py`
3. Update `CHANGELOG.md` (move [Unreleased] to new version)
4. Create a git commit with version bump
5. Create a git tag (e.g., `v1.0.1`)
6. Push commit and tag to GitHub

### Testing

```bash
# Dry-run to validate configuration
source test-env-vars.sh && python3 iwb-akash-deploy.py --dry-run

# Debug mode with actual deployment
source test-env-vars.sh && python3 iwb-akash-deploy.py -f test.yml --debug
```
