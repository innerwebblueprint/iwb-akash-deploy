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

## Installation

1. Clone the repository:
```bash
git clone https://github.com/innerwebblueprint/iwb-akash-deploy.git
cd iwb-akash-deploy
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

### Basic Usage

```bash
# Source environment variables
source test-env-vars.sh

# Deploy with SDL file
python3 iwb-akash-deploy.py -f test.yml
```

### Environment Variables

Required environment variables:
- `COMPOSE_PROJECT_NAME`: Project identifier (e.g., `tdk`)
- `IWB_STORJ_WPOPS_BUCKET`: Storj bucket name (optional)
- `IWB_DOMAIN`: Domain name (optional)

### Command Line Options

```bash
python3 iwb-akash-deploy.py [OPTIONS]

options:
  -h, --help            show this help message and exit
  --debug               Enable debug mode
  --dry-run             Validate without deploying
  --close               Close active deployment
  --status              Check lease status
  --logs                View deployment logs
  --shell               Get interactive shell into container
  --rpc-info            Show RPC info
  -y YAML, --yaml YAML  Custom YAML manifest
  -f YAML_FILE, --yaml-file YAML_FILE
                        Path to YAML file

