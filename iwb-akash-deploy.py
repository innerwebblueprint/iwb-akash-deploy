#!/usr/bin/env python3
"""
iwb-akash-deploy.py - Compact Akash Deployment Script
Deploy AI compute instances to Akash Network, specifically designed for use within n8n workflows
"""

__version__ = "1.1.9"

import argparse
import concurrent.futures
import json
import logging
import os
import secrets
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import time
import traceback
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# Configuration
compose_project = os.getenv('COMPOSE_PROJECT_NAME')
if not compose_project:
    print("Environment variables available:")
    for key in sorted(os.environ.keys()):
        if 'COMPOSE' in key:
            print(f"  {key}={os.environ[key]}")
    raise ValueError("COMPOSE_PROJECT_NAME environment variable must be set. Use 'export COMPOSE_PROJECT_NAME=your_value' in your shell.")

AKASH_WALLET_NAME = compose_project + 'akashwallet'
AKASH_KEYRING_BACKEND = 'test'
AKASH_CHAIN_ID = 'akashnet-2'
AKASH_RPC_NODES = [
    'https://rpc.akashnet.net:443',
    'https://rpc-akash.ecostake.com:443',
    'https://akash-rpc.polkachu.com:443',
    'https://akash.c29r3.xyz:443/rpc',
    'https://akash-rpc.europlots.com:443'
]
AKASH_NODE_FALLBACK = 'https://rpc.akashnet.net:443'
COMFYUI_PORT = 8188
DEFAULT_GAS_CONFIG = {'gas': 'auto', 'gas_adjustment': '1.75', 'gas_prices': '0.025uakt'}
DEFAULT_DEPLOYMENT_DEPOSIT_UACT = int(os.getenv('IWB_DEPLOYMENT_DEPOSIT_UACT', '5000000'))
DEFAULT_ACT_TOPUP_USD = float(os.getenv('IWB_ACT_TOPUP_USD', '2.0'))
DEFAULT_AKT_GAS_RESERVE_UAKT = int(os.getenv('IWB_AKT_GAS_RESERVE_UAKT', '500000'))


def strip_cli_warnings(output):
    """
    Remove known CLI warning lines from Akash CLI output before parsing as JSON or YAML.
    Returns only the lines that are likely to be valid JSON/YAML.
    """
    warning_prefixes = [
        'Warning:',
        'I[',  # Tendermint info log lines
        'E[',  # Tendermint error log lines
        'minimum-gas-prices is not set',
        'DEPRECATED:',
        'WRN ',
        'Error: ',
    ]
    clean_lines = []
    for line in output.splitlines():
        if any(line.strip().startswith(prefix) for prefix in warning_prefixes):
            continue
        if not line.strip():
            continue
        clean_lines.append(line)
    return '\n'.join(clean_lines)


class AkashDeployer:
    """Main deployer class - compact version"""
    
    def __init__(self, debug_mode=False, dseq=None, yaml_content=None, yaml_file=None):
        self.debug_mode = debug_mode
        self.is_dry_run = False  # Flag to track if we're in dry-run mode
        self.dseq = dseq  # Set dseq early so _setup_logging can use it
        self.yaml_content = yaml_content
        self.yaml_file = yaml_file
        self.wallet_address = None
        self.wallet_mnemonic = None
        self.balance_uakt = 0
        self.balance_uact = 0
        self.last_act_conversion = {
            'conversion_performed': False,
            'message': 'No ACT conversion performed'
        }
        self.akash_node = None  # Will be set after logger initialization
        self.logger = self._setup_logging()  # Will use self.dseq if provided
        self.state_file = self._get_state_file()
        self._temp_manifest_files = []
        # Now select RPC node with proper logging
        self.akash_node = self._select_fastest_rpc_node()

    def _setup_logging(self):
        log_file = self._get_log_file_path()
        self.current_log_file = log_file
        level = logging.DEBUG if self.debug_mode else logging.INFO
        handlers: List[logging.Handler] = [logging.FileHandler(log_file, mode='a')]
        if self.debug_mode:
            handlers.append(logging.StreamHandler(sys.stderr))
        logging.basicConfig(level=level, format='%(asctime)s - %(levelname)s - %(message)s', handlers=handlers)
        logger = logging.getLogger(__name__)
        logger.info("=" * 50)
        return logger

    def _get_log_file_path(self, dseq=None):
        """Get log file path - prefer user's home directory"""
        home = os.getenv('HOME')
        if home:
            try:
                base_dir = Path(home)
                # Test write access
                test_file = base_dir / ".write_test"
                test_file.touch()
                test_file.unlink()
            except (PermissionError, OSError):
                base_dir = Path(".")
        else:
            base_dir = Path(".")
        
        # Use dseq if provided, otherwise use self.dseq, otherwise use timestamp
        if dseq:
            return str(base_dir / f"iwb-akash-deploy_{dseq}.log")
        elif self.dseq:
            return str(base_dir / f"iwb-akash-deploy_{self.dseq}.log")
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            return str(base_dir / f"iwb-akash-deploy_{timestamp}_temp.log")

    def _switch_to_dseq_log_file(self, dseq):
        """Switch logging to a dseq-specific log file"""
        if not dseq:
            return
        
        # Update self.dseq if not already set
        if not self.dseq:
            self.dseq = dseq
        
        # Get new log file path
        new_log_file = self._get_log_file_path(dseq=dseq)
        
        # Check if we're already using this log file
        if self.current_log_file == new_log_file:
            return
        
        old_log_file = self.current_log_file
        
        # Log the transition
        self.logger.info(f"📝 Switching to DSEQ-specific log file: {new_log_file}")
        
        # Remove existing file handlers
        logger = logging.getLogger(__name__)
        for handler in logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                logger.removeHandler(handler)
        
        # Copy existing logs to new file
        try:
            if os.path.exists(old_log_file):
                with open(old_log_file, 'r') as old_f:
                    with open(new_log_file, 'a') as new_f:
                        new_f.write(old_f.read())
        except Exception as e:
            # If copy fails, at least log the error to stderr
            print(f"Warning: Failed to copy logs from {old_log_file} to {new_log_file}: {e}", file=sys.stderr)
        
        # Add new file handler
        new_handler = logging.FileHandler(new_log_file, mode='a')
        new_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(new_handler)
        
        # Update current log file
        self.current_log_file = new_log_file
        
        # Log the completion
        self.logger.info(f"✅ Now logging to: {new_log_file}")
        
        # Clean up old temporary log file
        try:
            if old_log_file != new_log_file and '_temp.log' in old_log_file and os.path.exists(old_log_file):
                os.remove(old_log_file)
                self.logger.info(f"🗑️  Removed temporary log file: {old_log_file}")
        except Exception as e:
            self.logger.warning(f"⚠️  Failed to remove temporary log file {old_log_file}: {e}")

    def _get_state_file(self):
        """Get state file path - prefer user's home directory"""
        home = os.getenv('HOME')
        if home:
            try:
                state_file = Path(home) / "active-deployment.json"
                # Test write access
                state_file.touch()
                return state_file
            except (PermissionError, OSError):
                pass
        
        # Fallback to current directory
        return Path("./active-deployment.json")

    def _error_response(self, error, deployment_info=None, lease_info=None, service_url=None, api_credentials=None, **kwargs):
        """Create standardized error response dict"""
        response = {
            'success': False,
            'message': 'Deployment failed',
            'error': error,
            'deployment_info': deployment_info,
            'lease_info': lease_info,
            'service_url': service_url,
            'api_credentials': api_credentials
        }
        response.update(kwargs)  # Allow additional fields
        return response
    
    def _ensure_wallet_and_deployment(self):
        """Helper to restore wallet and get active deployment. Returns (success, deployment_info, error_response)"""
        if not self.restore_wallet():
            return False, None, {'success': False, 'error': 'Wallet restoration failed'}
        
        has_active, deployment_info = self.has_active_deployment()
        if not has_active or not deployment_info:
            return False, None, {'success': False, 'error': 'No active deployment found'}
        
        return True, deployment_info, None

    def _select_fastest_rpc_node(self):
        """Select fastest RPC node with proper logging"""
        self.logger.info("🔍 Testing RPC node connectivity and speed...")
        
        def test_rpc_functionality(node_url, timeout=8):
            try:
                # First test basic connectivity
                start = time.time()
                response = requests.get(f"{node_url}/status", timeout=3)
                if response.status_code != 200:
                    return float('inf')
                
                # Then test actual blockchain query functionality
                # Use a simple query that should work on any Akash node
                test_cmd = ['provider-services', 'query', 'block', '--node', node_url]
                result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=timeout)
                
                if result.returncode == 0:
                    elapsed = time.time() - start
                    return elapsed
                else:
                    return float('inf')
                    
            except Exception as e:
                return float('inf')

        # Test nodes concurrently
        working_nodes = {}
        failed_nodes = []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(AKASH_RPC_NODES)) as executor:
            futures = {executor.submit(test_rpc_functionality, node): node for node in AKASH_RPC_NODES}
            
            for future in concurrent.futures.as_completed(futures):
                node = futures[future]
                try:
                    response_time = future.result()
                    if response_time < float('inf'):
                        working_nodes[node] = response_time
                        if self.debug_mode:
                            self.logger.debug(f"  ✅ {node}: {response_time:.3f}s")
                    else:
                        failed_nodes.append(node)
                        if self.debug_mode:
                            self.logger.debug(f"  ❌ {node}: Not responding")
                except Exception as e:
                    failed_nodes.append(node)
                    if self.debug_mode:
                        self.logger.debug(f"  ❌ {node}: {str(e)[:50]}")

        if working_nodes:
            # Select fastest working node
            selected_node = min(working_nodes.keys(), key=lambda x: working_nodes[x])
            self.logger.info(f"✅ Selected RPC node: {selected_node} ({working_nodes[selected_node]:.3f}s, {len(working_nodes)}/{len(AKASH_RPC_NODES)} nodes working)")
            
            if self.debug_mode and failed_nodes:
                self.logger.debug(f"   Failed nodes: {', '.join([n.split('//')[1].split(':')[0] for n in failed_nodes])}")
            
            return selected_node
        else:
            self.logger.warning(f"⚠️  All RPC nodes failed, using fallback: {AKASH_NODE_FALLBACK}")
            return AKASH_NODE_FALLBACK

    def run_command(self, cmd, timeout=30, env=None):
        if self.debug_mode:
            # Never log commands that might contain sensitive data
            cmd_str = ' '.join(cmd)
            if any(sensitive in cmd_str.lower() for sensitive in ['mnemonic', 'password', 'key', 'seed']):
                self.logger.debug("🔧 Executing: [SENSITIVE COMMAND HIDDEN]")
            else:
                self.logger.debug(f"🔧 Executing: {cmd_str}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env or os.environ)
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", "Command timed out", -1
        except Exception as e:
            return "", str(e), -1

    def build_akash_command(self, args, needs_gas=False, use_mtls=False, extra_flags=None, needs_keyring=True):
        """Build provider-services command"""
        cmd = ['provider-services'] + args

        # Add RPC node for all commands that need blockchain connection
        if any(x in args for x in ['query', 'tx']) or needs_gas:
            cmd.extend(['--node', self.akash_node])

        if needs_keyring:
            cmd.extend(['--keyring-backend', AKASH_KEYRING_BACKEND])
        if needs_gas or ('lease-status' in args):
            cmd.extend(['--from', AKASH_WALLET_NAME])
        if needs_gas:
            cmd.extend(['--chain-id', AKASH_CHAIN_ID, '--gas', 'auto', '--gas-adjustment', '1.75', '--gas-prices', '0.025uakt', '--yes'])
        if use_mtls:
            cmd.extend(['--auth-type', 'mtls'])
        if extra_flags:
            for k, v in extra_flags.items():
                cmd.extend([f'--{k}', v])
        return cmd

    def execute_tx(self, tx_args, **kwargs):
        """Execute transaction"""
        cmd = self.build_akash_command(tx_args, needs_gas=True, **kwargs)
        stdout, stderr, returncode = self.run_command(cmd, timeout=120)
        return returncode == 0, stdout, stderr

    def execute_query(self, query_args, **kwargs):
        """Execute query with automatic RPC failover"""
        needs_keyring = any(x in query_args for x in ['keys', 'lease-status', 'lease-shell'])
        
        # Try current node first
        cmd = self.build_akash_command(query_args, needs_keyring=needs_keyring, **kwargs)
        stdout, stderr, returncode = self.run_command(cmd, timeout=30)
        
        # If query failed and it was a blockchain query, try failover
        if returncode != 0 and any(x in query_args for x in ['query', 'tx']):
            self.logger.warning(f"⚠️  Query failed on {self.akash_node}, trying failover nodes...")
            
            # Try other nodes
            for backup_node in AKASH_RPC_NODES:
                if backup_node != self.akash_node:
                    self.logger.info(f"🔄 Trying backup node: {backup_node}")
                    
                    # Temporarily switch node for this query
                    original_node = self.akash_node
                    self.akash_node = backup_node
                    
                    cmd = self.build_akash_command(query_args, needs_keyring=needs_keyring, **kwargs)
                    stdout, stderr, returncode = self.run_command(cmd, timeout=30)
                    
                    if returncode == 0:
                        self.logger.info(f"✅ Query succeeded on backup node: {backup_node}")
                        # Update our primary node to the working one
                        break
                    else:
                        # Restore original node for next attempt
                        self.akash_node = original_node
        
        if returncode == 0:
            cleaned = strip_cli_warnings(stdout)
            try:
                return True, json.loads(cleaned)
            except json.JSONDecodeError:
                try:
                    return True, yaml.safe_load(cleaned)
                except yaml.YAMLError:
                    return True, cleaned
        return False, stderr

    def restore_wallet(self):
        """Restore wallet from backup"""
        self.logger.info("🔐 Restoring wallet from backup...")
        
        # Check if wallet exists
        if self.debug_mode:
            self.logger.debug(f"   Checking for existing wallet: {AKASH_WALLET_NAME}")
        
        success, result = self.execute_query(['keys', 'list', '--output', 'json'])
        if success and isinstance(result, list):
            for key in result:
                if key.get('name') == AKASH_WALLET_NAME:
                    self.wallet_address = key.get('address')
                    self.logger.info(f"✅ Wallet already exists: {self.wallet_address}")
                    self.balance_uakt = self.get_wallet_balance()
                    return True

        # Try restoration from Storj
        self.logger.info("   Wallet not found in keyring, restoring from Storj backup...")
        
        try:
            storj_bucket = os.getenv('IWB_STORJ_WPOPS_BUCKET')
            domain = os.getenv('IWB_DOMAIN')
            if not all([storj_bucket, domain]):
                self.logger.error("❌ Missing Storj environment variables (IWB_STORJ_WPOPS_BUCKET, IWB_DOMAIN)")
                return False

            # Download and extract backup
            backup_filename = f"{domain}_akash_latest.tar.gz"
            storj_path = f"sj://{storj_bucket}/IWBDPP/akash/latest/{backup_filename}"
            temp_dir = "/tmp/iwb-akash-restore"
            os.makedirs(temp_dir, exist_ok=True)

            # Download
            if self.debug_mode:
                self.logger.debug(f"   Downloading backup from: {storj_path}")
            
            stdout, stderr, rc = self.run_command(['uplink', 'cp', storj_path, f"{temp_dir}/{backup_filename}"], 60)
            if rc != 0:
                self.logger.error(f"❌ Failed to download backup from Storj: {stderr}")
                return False

            # Extract
            if self.debug_mode:
                self.logger.debug(f"   Extracting backup archive...")
            
            stdout, stderr, rc = self.run_command(['tar', '-xzf', f"{temp_dir}/{backup_filename}", '-C', temp_dir], 30)
            if rc != 0:
                self.logger.error(f"❌ Failed to extract backup: {stderr}")
                return False

            # Read wallet data
            wallet_file = f"{temp_dir}/{compose_project}_akash-deploy-backup.json"
            if self.debug_mode:
                self.logger.debug(f"   Reading wallet data from: {wallet_file}")
            
            with open(wallet_file, 'r') as f:
                wallet_data = json.load(f)

            mnemonic = wallet_data.get('mnemonic')
            if not mnemonic:
                self.logger.error("❌ No mnemonic found in backup file")
                return False
            
            # Store mnemonic for future backups (will be used by create_wallet_backup)
            self.wallet_mnemonic = mnemonic

            # Restore wallet (securely - don't log mnemonic)
            self.logger.info("   Importing wallet into keyring...")
            if self.debug_mode:
                self.logger.debug("🔧 Executing: provider-services keys add [WALLET_NAME] --recover --keyring-backend test --interactive=false (mnemonic passed securely via stdin)")
            
            # Use subprocess.Popen to securely pass mnemonic via stdin without logging it
            process = None
            try:
                process = subprocess.Popen(
                    ['provider-services', 'keys', 'add', AKASH_WALLET_NAME, '--recover', '--keyring-backend', 'test', '--interactive=false'],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                stdout, stderr = process.communicate(input=mnemonic, timeout=30)
                rc = process.returncode
                
                if rc != 0:
                    self.logger.error(f"❌ Wallet import failed: {stderr}")
                    return False
                    
            except subprocess.TimeoutExpired:
                if process:
                    process.kill()
                self.logger.error("❌ Wallet restoration timed out")
                return False
            except Exception as e:
                self.logger.error(f"❌ Wallet restoration failed: {e}")
                return False

            # Get wallet address from backup or query keyring
            self.wallet_address = wallet_data.get('address')
            if not self.wallet_address:
                if self.debug_mode:
                    self.logger.debug("   Querying keyring for wallet address...")
                success, result = self.execute_query(['keys', 'show', AKASH_WALLET_NAME, '--output', 'json'])
                if success and isinstance(result, dict):
                    self.wallet_address = result.get('address')

            # Restore certificate file from backup if it exists
            home_dir = os.path.expanduser("~")
            cert_dir = os.path.join(home_dir, ".akash")
            os.makedirs(cert_dir, exist_ok=True)
            
            pem_backup = f"{temp_dir}/{self.wallet_address}.pem"
            if os.path.exists(pem_backup):
                pem_dest = os.path.join(cert_dir, f"{self.wallet_address}.pem")
                if self.debug_mode:
                    self.logger.debug(f"   Restoring certificate file: {pem_backup} -> {pem_dest}")
                shutil.copy2(pem_backup, pem_dest)
                self.logger.info("✅ Certificate file restored from backup")
            else:
                if self.debug_mode:
                    self.logger.debug(f"   No certificate file found in backup at: {pem_backup}")

            self.logger.info(f"✅ Wallet restored successfully: {self.wallet_address}")
            
            # Cleanup
            if self.debug_mode:
                self.logger.debug("   Cleaning up temporary files...")
            self.run_command(['rm', '-rf', temp_dir], 10)
            return True

        except Exception as e:
            self.logger.error(f"❌ Wallet restoration failed: {e}")
            if self.debug_mode:
                self.logger.debug(f"   Exception details: {traceback.format_exc()}")
            return False

    def cleanup_wallet(self):
        """Clean up wallet from keyring and certificate files for security"""
        try:
            self.logger.info("🧹 Cleaning up wallet from keyring for security...")
            cmd = ['provider-services', 'keys', 'delete', AKASH_WALLET_NAME, '--keyring-backend', AKASH_KEYRING_BACKEND, '--yes']
            stdout, stderr, returncode = self.run_command(cmd, timeout=10)
            
            # Also remove certificate file
            if self.wallet_address:
                home_dir = os.path.expanduser("~")
                cert_dir = os.path.join(home_dir, ".akash")
                pem_file = os.path.join(cert_dir, f"{self.wallet_address}.pem")
                
                if os.path.exists(pem_file):
                    os.remove(pem_file)
                    if self.debug_mode:
                        self.logger.debug(f"   Removed certificate file: {pem_file}")
                    self.logger.info("✅ Certificate file removed")
            
            if returncode == 0:
                self.logger.info("✅ Wallet cleaned from keyring")
                return True
            else:
                self.logger.warning(f"⚠️  Wallet cleanup returned: {returncode} (may not have existed)")
                return False
        except Exception as e:
            self.logger.error(f"❌ Wallet cleanup failed: {e}")
            return False

    def create_wallet_backup(self):
        """Create unified backup (wallet + certificate) and upload to Storj"""
        try:
            if not self.wallet_address:
                self.logger.error("Cannot create backup without wallet address")
                return False
            
            self.logger.info("💾 Creating wallet backup (wallet + certificate)...")
            
            # Get configuration
            storj_bucket = os.getenv('IWB_STORJ_WPOPS_BUCKET')
            domain = os.getenv('IWB_DOMAIN')
            
            if not all([storj_bucket, domain]):
                self.logger.error("❌ Missing Storj environment variables (IWB_STORJ_WPOPS_BUCKET, IWB_DOMAIN)")
                return False
            
            # Create temporary backup directory
            temp_dir = "/tmp/iwb-akash-backup"
            os.makedirs(temp_dir, exist_ok=True)
            
            try:
                # 1. Get wallet mnemonic (if available from restoration)
                if hasattr(self, 'wallet_mnemonic') and self.wallet_mnemonic:
                    mnemonic = self.wallet_mnemonic
                else:
                    # Try to export mnemonic from keyring
                    if self.debug_mode:
                        self.logger.debug("   Exporting mnemonic from keyring...")
                    success, result = self.execute_query(['keys', 'export', AKASH_WALLET_NAME, '--unsafe', '--unarmored-hex'])
                    if success and isinstance(result, str):
                        mnemonic = result.strip()
                    else:
                        self.logger.warning("⚠️  Could not export mnemonic for backup")
                        mnemonic = None
                
                # 2. Create wallet backup JSON
                backup_file = f"{temp_dir}/{compose_project}_akash-deploy-backup.json"
                wallet_data = {
                    "walletName": AKASH_WALLET_NAME,
                    "address": self.wallet_address,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "network": "akash",
                    "chainId": AKASH_CHAIN_ID
                }
                
                if mnemonic:
                    wallet_data["mnemonic"] = mnemonic
                
                with open(backup_file, 'w') as f:
                    json.dump(wallet_data, f, indent=2)
                
                if self.debug_mode:
                    self.logger.debug(f"   Created wallet backup JSON: {backup_file}")
                
                # 3. Copy certificate file if it exists
                home_dir = os.path.expanduser("~")
                cert_dir = os.path.join(home_dir, ".akash")
                cert_filename = f"{self.wallet_address}.pem"
                cert_source = os.path.join(cert_dir, cert_filename)
                
                if os.path.exists(cert_source):
                    cert_dest = f"{temp_dir}/{cert_filename}"
                    shutil.copy2(cert_source, cert_dest)
                    if self.debug_mode:
                        self.logger.debug(f"   Copied certificate: {cert_filename}")
                else:
                    self.logger.warning(f"⚠️  Certificate not found for backup: {cert_source}")
                
                # 4. Create tar.gz archive
                archive_name = f"{domain}_akash_latest.tar.gz"
                archive_path = f"/tmp/{archive_name}"
                
                if self.debug_mode:
                    self.logger.debug(f"   Creating archive: {archive_path}")
                
                stdout, stderr, rc = self.run_command(['tar', '-czf', archive_path, '-C', temp_dir, '.'], 30)
                if rc != 0:
                    self.logger.error(f"❌ Failed to create backup archive: {stderr}")
                    return False
                
                # 5. Upload to Storj
                storj_path = f"sj://{storj_bucket}/IWBDPP/akash/latest/{archive_name}"
                if self.debug_mode:
                    self.logger.debug(f"   Uploading to: {storj_path}")
                
                stdout, stderr, rc = self.run_command(['uplink', 'cp', archive_path, storj_path], 60)
                if rc != 0:
                    self.logger.error(f"❌ Failed to upload backup to Storj: {stderr}")
                    return False
                
                self.logger.info(f"✅ Wallet backup uploaded to Storj: {storj_path}")
                
                # 6. Cleanup temp files
                if self.debug_mode:
                    self.logger.debug("   Cleaning up temporary backup files...")
                self.run_command(['rm', '-rf', temp_dir], 10)
                self.run_command(['rm', '-f', archive_path], 10)
                
                return True
                
            except Exception as e:
                # Cleanup on error
                self.run_command(['rm', '-rf', temp_dir], 10)
                self.run_command(['rm', '-f', f"/tmp/{domain}_akash_latest.tar.gz"], 10)
                raise e
                
        except Exception as e:
            self.logger.error(f"❌ Wallet backup creation failed: {e}")
            if self.debug_mode:
                self.logger.debug(f"   Exception details: {traceback.format_exc()}")
            return False

    def get_wallet_balance(self):
        """Get wallet balance"""
        if not self.wallet_address:
            return 0
        success, result = self.execute_query(['query', 'bank', 'balances', self.wallet_address])
        
        if self.debug_mode:
            self.logger.debug(f"Balance query result: success={success}, result={result}")
            
        if success and isinstance(result, dict):
            balances = result.get('balances', [])
            if self.debug_mode:
                self.logger.debug(f"Found {len(balances)} balance entries: {balances}")
            self.balance_uakt = 0
            self.balance_uact = 0
            for balance in balances:
                denom = balance.get('denom')
                amount = int(balance.get('amount', 0))
                if denom == 'uakt':
                    self.balance_uakt = amount
                elif denom == 'uact':
                    self.balance_uact = amount

            self.logger.info(f"💰 Balance: {self.balance_uakt / 1000000:.2f} AKT | {self.balance_uact / 1000000:.2f} ACT")
            return self.balance_uakt
        else:
            self.logger.error(f"Failed to get balance: success={success}, result={result}")
        return 0

    def get_act_balance(self):
        """Get ACT (uact) balance"""
        if not self.wallet_address:
            return 0

        self.get_wallet_balance()
        return self.balance_uact

    def get_bme_params(self):
        """Get BME module parameters"""
        success, result = self.execute_query(['query', 'bme', 'params'])
        if success and isinstance(result, dict):
            return result.get('params', {}) if isinstance(result.get('params', {}), dict) else {}
        return {}

    def get_bme_min_mint_uact(self):
        """Get minimum ACT mint amount in uact from BME params"""
        params = self.get_bme_params()
        min_mint_entries = params.get('min_mint', []) if isinstance(params, dict) else []

        if isinstance(min_mint_entries, list):
            for entry in min_mint_entries:
                if isinstance(entry, dict) and entry.get('denom') == 'uact':
                    try:
                        return int(entry.get('amount', 0))
                    except (TypeError, ValueError):
                        return 0
        return 0

    def get_bme_mint_spread_bps(self):
        """Get BME mint spread in basis points"""
        params = self.get_bme_params()
        try:
            return int(params.get('mint_spread_bps', 0)) if isinstance(params, dict) else 0
        except (TypeError, ValueError):
            return 0

    def _get_bme_ledger_record_for_height(self, owner, height):
        """Get BME ledger record for an owner at a specific block height"""
        if not owner or not height:
            return None

        success, result = self.execute_query([
            'query', 'bme', 'ledger',
            '--owner', owner,
            '--limit', '20',
            '--reverse'
        ])

        if not success or not isinstance(result, dict):
            return None

        records = result.get('records', [])
        for record in records:
            record_height = record.get('id', {}).get('height')
            if str(record_height) == str(height):
                return record

        return None

    def _extract_minted_uact_from_ledger_record(self, ledger_record):
        """Extract minted uact amount from an executed BME ledger record"""
        if not isinstance(ledger_record, dict):
            return 0

        executed_record = ledger_record.get('executed_record', {})
        if not isinstance(executed_record, dict):
            return 0

        minted = executed_record.get('minted', {})
        if not isinstance(minted, dict):
            return 0

        minted_coin = minted.get('coin', {})
        if not isinstance(minted_coin, dict):
            return 0

        if minted_coin.get('denom') != 'uact':
            return 0

        try:
            return int(minted_coin.get('amount', 0))
        except (TypeError, ValueError):
            return 0

    def _normalize_manifest_for_bme(self, manifest):
        """Normalize manifest pricing denom to uact for BME-compatible deployments"""
        changed = False
        if not isinstance(manifest, dict):
            return manifest, changed

        profiles = manifest.get('profiles', {})
        placement = profiles.get('placement', {}) if isinstance(profiles, dict) else {}

        if isinstance(placement, dict):
            for _, placement_entry in placement.items():
                if not isinstance(placement_entry, dict):
                    continue
                pricing = placement_entry.get('pricing', {})
                if not isinstance(pricing, dict):
                    continue
                for _, service_pricing in pricing.items():
                    if not isinstance(service_pricing, dict):
                        continue
                    if service_pricing.get('denom') == 'uakt':
                        service_pricing['denom'] = 'uact'
                        changed = True

        return manifest, changed

    def _write_manifest_temp_file(self, manifest_obj):
        """Write manifest object to temporary file and return file path"""
        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', prefix='iwb-deploy-', delete=False)
        yaml.safe_dump(manifest_obj, temp_file, default_flow_style=False, sort_keys=False)
        temp_file.close()
        self._temp_manifest_files.append(temp_file.name)
        return temp_file.name

    def ensure_act_for_deployment(self, required_uact=DEFAULT_DEPLOYMENT_DEPOSIT_UACT):
        """Ensure wallet has enough ACT for deployment deposit by minting from AKT when needed"""
        self.get_wallet_balance()
        akt_balance_before = self.balance_uakt
        act_balance = self.balance_uact

        if act_balance >= required_uact:
            self.logger.info(f"✅ ACT balance sufficient for deployment: {act_balance / 1000000:.2f} ACT")
            self.last_act_conversion = {
                'conversion_performed': False,
                'required_uact': required_uact,
                'required_act': required_uact / 1_000_000,
                'act_balance_before_uact': act_balance,
                'act_balance_before_act': act_balance / 1_000_000,
                'akt_balance_before_uakt': akt_balance_before,
                'akt_balance_before_akt': akt_balance_before / 1_000_000,
                'message': 'ACT balance already sufficient; no conversion needed'
            }
            return True

        deficit_uact = max(required_uact - act_balance, 0)
        bme_min_mint_uact = self.get_bme_min_mint_uact()
        mint_spread_bps = self.get_bme_mint_spread_bps()
        spread_multiplier = max(1 - (mint_spread_bps / 10_000), 0.0001)

        target_mint_uact = max(deficit_uact, int(DEFAULT_ACT_TOPUP_USD * 1_000_000), bme_min_mint_uact)
        target_mint_usd = target_mint_uact / 1_000_000

        if bme_min_mint_uact > 0 and target_mint_uact == bme_min_mint_uact and deficit_uact < bme_min_mint_uact:
            self.logger.info(
                f"ℹ️ BME minimum mint applies: {bme_min_mint_uact / 1000000:.2f} ACT minimum per mint request"
            )

        akt_price = self.get_akt_price()
        if akt_price and akt_price > 0:
            burn_uakt = int((target_mint_usd / (akt_price * spread_multiplier)) * 1_000_000)
            burn_uakt = max(burn_uakt, 1)
        else:
            # Fallback if price lookup fails: burn 2 AKT to mint ACT
            burn_uakt = 2_000_000
            self.logger.warning("⚠️ Could not fetch AKT/USD price; falling back to burning 2.0 AKT for ACT top-up")

        available_uakt = self.balance_uakt
        minimum_needed_uakt = burn_uakt + DEFAULT_AKT_GAS_RESERVE_UAKT
        if available_uakt < minimum_needed_uakt:
            self.logger.error(
                f"❌ Insufficient AKT for ACT mint + gas reserve (need {minimum_needed_uakt / 1000000:.2f} AKT, have {available_uakt / 1000000:.2f} AKT)"
            )
            self.last_act_conversion = {
                'conversion_performed': False,
                'required_uact': required_uact,
                'required_act': required_uact / 1_000_000,
                'act_balance_before_uact': act_balance,
                'act_balance_before_act': act_balance / 1_000_000,
                'akt_balance_before_uakt': available_uakt,
                'akt_balance_before_akt': available_uakt / 1_000_000,
                'burn_uakt_attempted': burn_uakt,
                'burn_akt_attempted': burn_uakt / 1_000_000,
                'message': 'Insufficient AKT for ACT conversion + gas reserve'
            }
            return False

        self.logger.info(
            f"🔄 ACT balance low ({act_balance / 1000000:.2f} ACT), minting ACT by burning {burn_uakt / 1000000:.4f} AKT..."
        )
        success, stdout, stderr = self.execute_tx(['tx', 'bme', 'mint-act', f'{burn_uakt}uakt'])
        if not success:
            self.logger.error(f"❌ ACT mint failed: {stderr}")
            self.last_act_conversion = {
                'conversion_performed': False,
                'required_uact': required_uact,
                'required_act': required_uact / 1_000_000,
                'act_balance_before_uact': act_balance,
                'act_balance_before_act': act_balance / 1_000_000,
                'akt_balance_before_uakt': akt_balance_before,
                'akt_balance_before_akt': akt_balance_before / 1_000_000,
                'burn_uakt_attempted': burn_uakt,
                'burn_akt_attempted': burn_uakt / 1_000_000,
                'message': f'ACT mint failed: {stderr.strip()}'
            }
            return False

        tx_hash = None
        tx_height = None
        tx_code = None
        try:
            tx_result = json.loads(strip_cli_warnings(stdout)) if stdout else {}
            if isinstance(tx_result, dict):
                tx_hash = tx_result.get('txhash')
                tx_height = tx_result.get('height')
                tx_code = tx_result.get('code')
        except Exception:
            tx_result = {}

        if tx_code not in (None, 0):
            self.logger.error(f"❌ ACT mint tx returned non-zero code: {tx_code}")
            self.last_act_conversion = {
                'conversion_performed': False,
                'required_uact': required_uact,
                'required_act': required_uact / 1_000_000,
                'act_balance_before_uact': act_balance,
                'act_balance_before_act': act_balance / 1_000_000,
                'akt_balance_before_uakt': akt_balance_before,
                'akt_balance_before_akt': akt_balance_before / 1_000_000,
                'burn_uakt_attempted': burn_uakt,
                'burn_akt_attempted': burn_uakt / 1_000_000,
                'tx_hash': tx_hash,
                'tx_height': tx_height,
                'tx_code': tx_code,
                'message': f'ACT mint tx failed with code {tx_code}'
            }
            return False

        ledger_record = None
        if tx_height:
            for attempt in range(1, 6):
                ledger_record = self._get_bme_ledger_record_for_height(self.wallet_address, tx_height)
                if ledger_record:
                    break
                if attempt < 6:
                    time.sleep(2)

        if ledger_record and ledger_record.get('status') == 'ledger_record_status_canceled':
            canceled_record = ledger_record.get('canceled_record', {}) if isinstance(ledger_record, dict) else {}
            cancel_reason = canceled_record.get('cancel_reason', 'unknown')
            self.logger.error(
                f"❌ ACT mint was canceled by BME ledger at height {tx_height}. "
                f"Reason: {cancel_reason}. txhash: {tx_hash}"
            )
            self.last_act_conversion = {
                'conversion_performed': False,
                'required_uact': required_uact,
                'required_act': required_uact / 1_000_000,
                'act_balance_before_uact': act_balance,
                'act_balance_before_act': act_balance / 1_000_000,
                'akt_balance_before_uakt': akt_balance_before,
                'akt_balance_before_akt': akt_balance_before / 1_000_000,
                'burn_uakt_attempted': burn_uakt,
                'burn_akt_attempted': burn_uakt / 1_000_000,
                'bme_min_mint_uact': bme_min_mint_uact,
                'bme_min_mint_act': bme_min_mint_uact / 1_000_000,
                'mint_spread_bps': mint_spread_bps,
                'target_mint_uact': target_mint_uact,
                'target_mint_act': target_mint_uact / 1_000_000,
                'tx_hash': tx_hash,
                'tx_height': tx_height,
                'ledger_status': ledger_record.get('status'),
                'ledger_cancel_reason': cancel_reason,
                'message': f'ACT mint canceled by BME ledger ({cancel_reason})'
            }
            return False

        ledger_status = ledger_record.get('status') if isinstance(ledger_record, dict) else None
        minted_uact_ledger = 0
        if ledger_status == 'ledger_record_status_executed':
            minted_uact_ledger = self._extract_minted_uact_from_ledger_record(ledger_record)
            if minted_uact_ledger > 0:
                self.logger.info(f"✅ BME ledger confirms executed mint: {minted_uact_ledger / 1000000:.6f} ACT")
        elif tx_height and not ledger_record:
            self.logger.warning(
                f"⚠️ Could not find BME ledger record at height {tx_height} after retry; falling back to bank balance check"
            )
        elif ledger_status and ledger_status != 'ledger_record_status_executed':
            self.logger.warning(
                f"⚠️ Unexpected BME ledger status for mint tx at height {tx_height}: {ledger_status}"
            )

        self.get_wallet_balance()
        refreshed_act_balance = self.balance_uact
        akt_balance_after = self.balance_uakt
        minted_uact_from_balance = max(refreshed_act_balance - act_balance, 0)
        minted_uact_effective = max(minted_uact_from_balance, minted_uact_ledger)
        effective_act_balance = max(refreshed_act_balance, act_balance + minted_uact_effective)

        self.last_act_conversion = {
            'conversion_performed': True,
            'required_uact': required_uact,
            'required_act': required_uact / 1_000_000,
            'act_balance_before_uact': act_balance,
            'act_balance_before_act': act_balance / 1_000_000,
            'act_balance_after_uact': refreshed_act_balance,
            'act_balance_after_act': refreshed_act_balance / 1_000_000,
            'akt_balance_before_uakt': akt_balance_before,
            'akt_balance_before_akt': akt_balance_before / 1_000_000,
            'akt_balance_after_uakt': akt_balance_after,
            'akt_balance_after_akt': akt_balance_after / 1_000_000,
            'burned_uakt': burn_uakt,
            'burned_akt': burn_uakt / 1_000_000,
            'minted_uact_estimate': minted_uact_effective,
            'minted_act_estimate': minted_uact_effective / 1_000_000,
            'minted_uact_from_balance': minted_uact_from_balance,
            'minted_uact_from_ledger': minted_uact_ledger,
            'effective_act_balance_uact': effective_act_balance,
            'effective_act_balance_act': effective_act_balance / 1_000_000,
            'bme_min_mint_uact': bme_min_mint_uact,
            'bme_min_mint_act': bme_min_mint_uact / 1_000_000,
            'mint_spread_bps': mint_spread_bps,
            'target_mint_uact': target_mint_uact,
            'target_mint_act': target_mint_uact / 1_000_000,
            'tx_hash': tx_hash,
            'tx_height': tx_height,
            'tx_code': tx_code,
            'ledger_status': ledger_status,
            'message': 'ACT conversion successful'
        }

        if effective_act_balance < required_uact:
            self.logger.error(
                f"❌ ACT still insufficient after mint (required {required_uact / 1000000:.2f} ACT, "
                f"effective {effective_act_balance / 1000000:.2f} ACT, bank-visible {refreshed_act_balance / 1000000:.2f} ACT)"
            )
            self.last_act_conversion['message'] = 'ACT conversion completed but balance still insufficient'
            return False

        if refreshed_act_balance < required_uact and effective_act_balance >= required_uact:
            self.logger.warning(
                f"⚠️ ACT appears sufficient via BME ledger execution ({effective_act_balance / 1000000:.2f} ACT), "
                f"but bank query currently shows {refreshed_act_balance / 1000000:.2f} ACT"
            )

        self.logger.info(
            f"✅ ACT mint successful. Effective ACT balance: {effective_act_balance / 1000000:.2f} ACT "
            f"(bank-visible: {refreshed_act_balance / 1000000:.2f} ACT)"
        )
        return True

    def setup_certificate(self):
        """Setup certificate - ensure both on-chain and local certificate files exist"""
        self.logger.info("🔐 Checking certificate status...")
        
        # Check if local certificate file exists
        home_dir = os.path.expanduser("~")
        cert_dir = os.path.join(home_dir, ".akash")
        pem_file = os.path.join(cert_dir, f"{self.wallet_address}.pem")
        
        local_cert_exists = os.path.exists(pem_file)
        
        if self.debug_mode:
            self.logger.debug(f"   Certificate directory: {cert_dir}")
            self.logger.debug(f"   PEM file exists: {local_cert_exists}")
        
        # Query certificates for this wallet on-chain
        success, result = self.execute_query(['query', 'cert', 'list', '--owner', self.wallet_address])

        # Akash Mainnet 14/provider-services v0.10.1: output may be dict with 'certificates', or a list, or other structure
        certs = []
        if success:
            if isinstance(result, dict):
                # New format: {'certificates': [ { certificate: {...}, state: 'valid', ... }, ... ]}
                if 'certificates' in result and isinstance(result['certificates'], list):
                    certs = result['certificates']
                # Some versions may return a single certificate as dict
                elif 'certificate' in result:
                    certs = [result]
            elif isinstance(result, list):
                certs = result

        # Find at least one valid certificate
        valid_certs = []
        for c in certs:
            # v0.10.1: each entry is { certificate: {...}, state: 'valid', ... }
            state = c.get('state')
            if not state and 'certificate' in c and isinstance(c['certificate'], dict):
                state = c.get('state') or c['certificate'].get('state')
            if state == 'valid':
                valid_certs.append(c)

        if valid_certs:
            self.logger.info(f"✅ Certificate already published ({len(valid_certs)} valid certificate(s) found for this wallet)")

            # If on-chain certificate exists but local file is missing, regenerate it
            if not local_cert_exists:
                # In dry-run mode, just report what would happen
                if self.is_dry_run:
                    self.logger.info("🧪 DRY-RUN: Would regenerate local certificate files during actual deployment")
                    return True

                self.logger.info("   Local certificate files missing, regenerating...")
                os.makedirs(cert_dir, exist_ok=True)

                # Generate local certificate files (this creates .pem and .crt files)
                # Note: This won't publish a new cert since one already exists on-chain
                success, stdout, stderr = self.execute_tx(['tx', 'cert', 'generate', 'client', '--overwrite'])
                if success:
                    self.logger.info("✅ Local certificate files regenerated successfully")
                    return True
                else:
                    self.logger.error(f"❌ Failed to regenerate local certificate files: {stderr}")
                    return False
            return True
        
        # No certificate on-chain - need to generate and publish
        # In dry-run mode, skip actual generation/publishing
        if self.is_dry_run:
            self.logger.info("🧪 DRY-RUN: Would generate and publish new certificate during actual deployment")
            self.logger.info("🧪 DRY-RUN: Certificate generation requires AKT for gas fees")
            return False  # Return False to indicate certificate doesn't exist yet
        
        self.logger.info("   Generating and publishing new certificate to blockchain...")
        os.makedirs(cert_dir, exist_ok=True)
        
        # First generate local certificate files
        success, stdout, stderr = self.execute_tx(['tx', 'cert', 'generate', 'client', '--overwrite'])
        if not success:
            self.logger.error(f"❌ Certificate generation failed: {stderr}")
            return False
        
        # Then publish to blockchain (this costs AKT)
        success, stdout, stderr = self.execute_tx(['tx', 'cert', 'publish', 'client'])
        if not success:
            self.logger.error(f"❌ Certificate publication failed: {stderr}")
            return False
        
        self.logger.info("✅ Certificate published successfully")
        
        # AFTER successful publish, create unified backup (wallet + certificate) and upload to Storj
        # This ensures we only backup if the publish succeeded (which costs AKT)
        if not self.create_wallet_backup():
            self.logger.warning("⚠️  Failed to create wallet backup with new certificate")
            # Don't fail the whole process if backup fails
        
        return True

    def _parse_certificate_entries(self, query_result):
        """Normalize certificate query output into a list of entries"""
        certs = []
        if isinstance(query_result, dict):
            if 'certificates' in query_result and isinstance(query_result['certificates'], list):
                certs = query_result['certificates']
            elif 'certificate' in query_result:
                certs = [query_result]
        elif isinstance(query_result, list):
            certs = query_result

        normalized = []
        for cert_entry in certs:
            if not isinstance(cert_entry, dict):
                continue

            cert_data_raw = cert_entry.get('certificate')
            cert_data = cert_data_raw if isinstance(cert_data_raw, dict) else cert_entry
            if not isinstance(cert_data, dict):
                cert_data = {}

            state = cert_entry.get('state') or cert_data.get('state') or 'unknown'
            serial = cert_entry.get('serial') or cert_data.get('serial')
            owner = cert_data.get('owner') or cert_entry.get('owner') or self.wallet_address

            normalized.append({
                'state': state,
                'serial': serial,
                'owner': owner,
                'certificate': cert_data,
                'raw': cert_entry,
            })

        return normalized

    def get_certificate_status(self, owner_address=None):
        """Get on-chain and local certificate status for an owner"""
        owner = owner_address or self.wallet_address
        if not owner:
            return {
                'success': False,
                'error': 'Owner wallet address not available',
                'owner': None,
                'certificates': []
            }

        success, result = self.execute_query(['query', 'cert', 'list', '--owner', owner])
        if not success:
            return {
                'success': False,
                'error': f'Certificate query failed: {result}',
                'owner': owner,
                'certificates': []
            }

        certificates = self._parse_certificate_entries(result)
        valid_certificates = [c for c in certificates if c.get('state') == 'valid']

        local_certificate_exists = False
        if owner == self.wallet_address and self.wallet_address:
            home_dir = os.path.expanduser("~")
            pem_file = os.path.join(home_dir, ".akash", f"{self.wallet_address}.pem")
            local_certificate_exists = os.path.exists(pem_file)

        return {
            'success': True,
            'owner': owner,
            'certificate_count': len(certificates),
            'valid_certificate_count': len(valid_certificates),
            'has_valid_certificate': len(valid_certificates) > 0,
            'local_certificate_exists': local_certificate_exists,
            'certificates': certificates,
        }

    def query_certificates(self, owner_address=None):
        """Query certificates for owner (wallet owner by default)"""
        status = self.get_certificate_status(owner_address=owner_address)
        if status.get('success'):
            self.logger.info(
                f"✅ Certificate query complete: {status['valid_certificate_count']}/{status['certificate_count']} valid for {status['owner']}"
            )
        else:
            self.logger.error(f"❌ Certificate query failed: {status.get('error')}")
        return status

    def add_certificate(self):
        """Ensure certificate exists (idempotent) using existing setup flow"""
        cert_ready = self.setup_certificate()
        status = self.get_certificate_status(owner_address=self.wallet_address)
        return {
            'success': cert_ready,
            'message': 'Certificate is ready' if cert_ready else 'Certificate setup failed',
            'certificate_status': status
        }

    def revoke_certificate(self, serial):
        """Revoke a specific certificate serial"""
        if not serial:
            return {'success': False, 'error': 'Certificate serial is required for revoke'}

        self.logger.info(f"🗑️  Revoking certificate serial: {serial}")
        success, stdout, stderr = self.execute_tx(['tx', 'cert', 'revoke', 'client', '--serial', str(serial)])
        if not success:
            self.logger.error(f"❌ Certificate revoke failed: {stderr}")
            return {'success': False, 'error': f'Certificate revoke failed: {stderr}', 'serial': serial}

        self.logger.info("✅ Certificate revoked successfully")
        return {
            'success': True,
            'serial': serial,
            'tx_result': stdout,
            'certificate_status': self.get_certificate_status(owner_address=self.wallet_address)
        }

    def create_new_certificate(self, overwrite=False):
        """Create and publish a new certificate, optionally replacing existing valid certs"""
        self.logger.info("🔐 Creating new certificate...")

        current_status = self.get_certificate_status(owner_address=self.wallet_address)
        if not current_status.get('success'):
            return current_status

        valid_certs = [c for c in current_status.get('certificates', []) if c.get('state') == 'valid']
        if valid_certs and not overwrite:
            message = 'Valid certificate already exists. Use --cert-overwrite with --cert-new to replace it.'
            self.logger.warning(f"⚠️  {message}")
            return {
                'success': False,
                'error': message,
                'certificate_status': current_status
            }

        if overwrite and valid_certs:
            self.logger.info(f"🔁 Overwrite enabled: revoking {len(valid_certs)} valid certificate(s) first...")
            for cert in valid_certs:
                serial = cert.get('serial')
                if not serial:
                    error = 'Cannot overwrite certificate because serial was not found in query response'
                    self.logger.error(f"❌ {error}")
                    return {
                        'success': False,
                        'error': error,
                        'certificate_status': current_status
                    }

                revoke_result = self.revoke_certificate(serial)
                if not revoke_result.get('success'):
                    return revoke_result

        home_dir = os.path.expanduser("~")
        cert_dir = os.path.join(home_dir, ".akash")
        os.makedirs(cert_dir, exist_ok=True)

        generate_cmd = ['tx', 'cert', 'generate', 'client', '--overwrite']
        success, stdout, stderr = self.execute_tx(generate_cmd)
        if not success:
            self.logger.error(f"❌ Certificate generation failed: {stderr}")
            return {'success': False, 'error': f'Certificate generation failed: {stderr}'}

        success, stdout, stderr = self.execute_tx(['tx', 'cert', 'publish', 'client'])
        if not success:
            self.logger.error(f"❌ Certificate publication failed: {stderr}")
            return {'success': False, 'error': f'Certificate publication failed: {stderr}'}

        backup_uploaded = self.create_wallet_backup()
        if not backup_uploaded:
            self.logger.warning("⚠️  New certificate published but wallet backup update failed")

        updated_status = self.get_certificate_status(owner_address=self.wallet_address)
        return {
            'success': True,
            'message': 'New certificate created and published successfully',
            'backup_uploaded': backup_uploaded,
            'certificate_status': updated_status
        }

    def create_deployment_manifest(self, api_credentials):
        """Return path to manifest file - use provided file from n8n or yaml content directly"""
        # If a YAML file was provided (e.g., from n8n at /tmp/deploy.yaml), normalize denom and use temp file
        if self.yaml_file:
            self.logger.info(f"📄 Using provided YAML file: {self.yaml_file}")
            try:
                with open(self.yaml_file, 'r') as f:
                    manifest = yaml.safe_load(strip_cli_warnings(f.read()))
                if not isinstance(manifest, dict):
                    raise ValueError('YAML manifest must be a mapping/object')
                manifest, changed = self._normalize_manifest_for_bme(manifest)
                manifest_path = self._write_manifest_temp_file(manifest)
                if changed:
                    self.logger.info(f"🧩 Normalized manifest pricing denom to uact for BME compatibility: {manifest_path}")
                return manifest_path
            except Exception as e:
                self.logger.warning(f"⚠️ Could not normalize YAML file, using original as-is: {e}")
                return self.yaml_file
        
        # If YAML content was provided as string, normalize denom and write to temp file
        if self.yaml_content:
            self.logger.info(f"📄 Using provided YAML content")
            try:
                manifest = yaml.safe_load(strip_cli_warnings(self.yaml_content))
                if not isinstance(manifest, dict):
                    raise ValueError('YAML manifest must be a mapping/object')
                manifest, changed = self._normalize_manifest_for_bme(manifest)
                manifest_path = self._write_manifest_temp_file(manifest)
                if changed:
                    self.logger.info(f"🧩 Normalized manifest pricing denom to uact for BME compatibility: {manifest_path}")
                return manifest_path
            except Exception as e:
                self.logger.warning(f"⚠️ Could not normalize YAML content, writing raw content to temp file: {e}")
                temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', prefix='iwb-deploy-raw-', delete=False)
                temp_file.write(self.yaml_content)
                temp_file.close()
                self._temp_manifest_files.append(temp_file.name)
                return temp_file.name
        
        # Should not reach here in n8n workflow, but provide default if needed
        self.logger.warning("⚠️  No YAML provided, this should not happen in n8n workflow")
        return None

    def generate_api_credentials(self, service_url=''):
        """Generate API credentials"""
        return {
            'username': 'comfyui_' + ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6)),
            'password': ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16)),
            'api_url': service_url or 'http://service-url-placeholder'
        }

    def send_email(self, subject, body):
        """Send email notification using system mail command"""
        try:
            mail_from = os.getenv('IWB_MAIL_USER', 'admin') + '@' + os.getenv('IWB_DOMAIN', 'localhost')
            result = subprocess.run(['mail', '-s', subject, '-r', mail_from, mail_from], 
                                  input=body, text=True, timeout=30, capture_output=True)
            if result.returncode == 0:
                self.logger.info("📧 Email sent successfully")
                return True
            self.logger.warning(f"⚠️ Email failed: {result.stderr}")
            return False
        except Exception as e:
            self.logger.warning(f"⚠️ Email error: {e}")
            return False

    def get_akt_price(self):
        """Get current AKT/USD price from CoinGecko, returns None if unavailable"""
        try:
            response = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=akash-network&vs_currencies=usd', timeout=10)
            if response.status_code == 200:
                price = response.json().get('akash-network', {}).get('usd')
                if price:
                    self.logger.info(f"💱 AKT/USD: ${price:.2f}")
                    return price
        except Exception as e:
            self.logger.warning(f"⚠️ Could not fetch AKT price: {e}")
        return None

    def save_state(self, deployment_info):
        """Save deployment state"""
        try:
            self.logger.debug(f"💾 Saving state to: {self.state_file}")
            with open(self.state_file, 'w') as f:
                json.dump({'deployment_info': deployment_info, 'created_at': datetime.now(timezone.utc).isoformat() + 'Z', 'status': 'active'}, f, indent=2)
            self.logger.debug(f"✅ State saved successfully")
            return True
        except Exception as e:
            self.logger.warning(f"⚠️  Failed to save state to {self.state_file}: {e}")
            return False

    def load_state(self):
        """Load deployment state"""
        try:
            return json.load(open(self.state_file)).get('deployment_info') if self.state_file.exists() else None
        except Exception:
            return None

    def clear_state(self):
        """Clear deployment state"""
        try:
            if self.state_file.exists(): self.state_file.unlink()
            return True
        except Exception:
            return False

    def _get_lease_info_for_deployment(self, dseq):
        """Query lease information for a deployment to get provider details"""
        try:
            success, result = self.execute_query(['query', 'market', 'lease', 'list', '--owner', self.wallet_address])
            
            if success and isinstance(result, dict):
                leases = result.get('leases', [])
                
                # Find lease for this deployment
                for lease_entry in leases:
                    lease = lease_entry.get('lease', {})
                    lease_id = lease.get('lease_id', {})
                    
                    if str(lease_id.get('dseq')) == str(dseq) and lease.get('state', '').lower() == 'active':
                        # Found active lease for this deployment
                        return {
                            'provider': lease_id.get('provider', ''),
                            'gseq': str(lease_id.get('gseq', '1')),
                            'oseq': str(lease_id.get('oseq', '1'))
                        }
            
            self.logger.debug(f"No active lease found for deployment {dseq}")
            return None
            
        except Exception as e:
            self.logger.warning(f"Error querying lease info for deployment {dseq}: {e}")
            return None

    def _query_bids(self, dseq, state_filter='open'):
        """Query bids for a deployment
        
        Args:
            dseq: Deployment sequence number
            state_filter: Filter by bid state ('open', 'closed', 'all')
                         'open' - only open bids
                         'closed' - only closed bids  
                         'all' - all bids regardless of state
        
        Returns:
            dict with 'open_bids', 'closed_bids', and 'all_bids' lists
            or None if query fails
        """
        try:
            # Query all bids (no state filter)
            success, result = self.execute_query([
                'query', 'market', 'bid', 'list', '--dseq', str(dseq), '--owner', self.wallet_address
            ])
            
            if not success or not isinstance(result, dict):
                # Try with state filter as fallback
                success, result = self.execute_query([
                    'query', 'market', 'bid', 'list', 
                    '--dseq', str(dseq), '--owner', self.wallet_address, '--state', state_filter
                ])
                
                if success and isinstance(result, dict):
                    bids = result.get('bids', [])
                    # Assume all returned bids match the state filter
                    if state_filter == 'open':
                        return {'open_bids': bids, 'closed_bids': [], 'all_bids': bids}
                    elif state_filter == 'closed':
                        return {'open_bids': [], 'closed_bids': bids, 'all_bids': bids}
                    else:
                        return {'open_bids': bids, 'closed_bids': [], 'all_bids': bids}
                
                return None
            
            # Parse and categorize bids
            all_bids = result.get('bids', [])
            open_bids = [bid for bid in all_bids if bid.get('bid', {}).get('state') == 'open']
            closed_bids = [bid for bid in all_bids if bid.get('bid', {}).get('state') == 'closed']
            
            return {
                'open_bids': open_bids,
                'closed_bids': closed_bids,
                'all_bids': all_bids
            }
            
        except Exception as e:
            self.logger.warning(f"Error querying bids for deployment {dseq}: {e}")
            return None

    def has_active_deployment(self):
        """Check for active deployment and validate it's still active"""
        # First check local state file
        deployment_info = self.load_state()
        if deployment_info and deployment_info.get('dseq'):
            dseq = deployment_info.get('dseq')
            owner = deployment_info.get('owner', self.wallet_address)
            
            # Validate the deployment is still active by querying it
            try:
                success, result = self.execute_query(['query', 'deployment', 'get', '--dseq', str(dseq), '--owner', owner])
                
                if success and isinstance(result, dict):
                    # Debug the full structure
                    self.logger.debug(f"Deployment query result: {json.dumps(result, indent=2)}")
                    
                    # Try different possible structures
                    deployment_data = result.get('deployment', {})
                    if isinstance(deployment_data, dict):
                        # Could be deployment.deployment or just deployment
                        deployment = deployment_data.get('deployment', deployment_data)
                        
                        # Try to get deployment_id and state
                        deployment_id = deployment.get('deployment_id', {})
                        state = deployment.get('state', '').lower()
                        
                        self.logger.debug(f"Parsed deployment - DSEQ: {deployment_id.get('dseq')}, State: '{state}'")
                        
                        if state == 'active':
                            self.logger.info(f"✅ Verified active deployment from state file: DSEQ {dseq}")
                            return True, deployment_info
                        else:
                            self.logger.info(f"🔄 Deployment {dseq} from state file is no longer active (state: '{state}'), clearing state")
                            self.clear_state()
                    else:
                        self.logger.warning(f"🔄 Unexpected deployment data structure, clearing state")
                        self.clear_state()
                else:
                    self.logger.info(f"🔄 Could not verify deployment {dseq} from state file, clearing state")
                    self.clear_state()
                    
            except Exception as e:
                self.logger.warning(f"Error validating deployment {dseq} from state file: {e}")
                self.logger.info(f"🔄 Error validating deployment {dseq}, clearing state")
                self.clear_state()
        
        # No valid state file or state was cleared, query blockchain for ANY active deployments
        self.logger.info("🔍 No valid local state, querying blockchain for active deployments...")
        if not self.wallet_address:
            self.logger.debug("Wallet address not available yet")
            return False, None
            
        try:
            success, result = self.execute_query(['query', 'deployment', 'list', '--owner', self.wallet_address])
            
            if success and isinstance(result, dict):
                deployments = result.get('deployments', [])
                self.logger.debug(f"Found {len(deployments)} total deployments for this wallet")
                
                # Look for active deployments
                for deployment_entry in deployments:
                    deployment = deployment_entry.get('deployment', {})
                    deployment_id = deployment.get('deployment_id', {})
                    state = deployment.get('state', '').lower()
                    dseq = deployment_id.get('dseq')
                    
                    if state == 'active' and dseq:
                        self.logger.info(f"✅ Found active deployment on blockchain: DSEQ {dseq}")
                        
                        # Query for lease information to get provider details
                        provider_info = self._get_lease_info_for_deployment(str(dseq))
                        
                        # Reconstruct deployment_info and save it
                        deployment_info = {
                            'dseq': str(dseq),
                            'owner': self.wallet_address,
                            'manifest_path': self.yaml_file or 'unknown'
                        }
                        
                        # Add lease/provider info if found
                        if provider_info:
                            deployment_info.update(provider_info)
                        
                        self.save_state(deployment_info)
                        
                        return True, deployment_info
                
                self.logger.debug("No active deployments found on blockchain")
                return False, None
            else:
                self.logger.debug("Failed to query deployments from blockchain")
                return False, None
                
        except Exception as e:
            self.logger.warning(f"Error querying blockchain for active deployments: {e}")
            return False, None

    def _parse_dseq_from_output(self, stdout):
        """Parse DSEQ from deployment creation output - tries JSON then text patterns"""
        import re
        dseq = None
        try:
            output_data = json.loads(strip_cli_warnings(stdout))
            if isinstance(output_data, dict):
                if output_data.get('txhash'):
                    self.logger.info(f"Got transaction hash: {output_data['txhash']}")

                # 1. Look for EventDeploymentCreated event
                events = output_data.get('events', [])
                for event in events:
                    if event.get('type') == 'akash.deployment.v1.EventDeploymentCreated':
                        for attr in event.get('attributes', []):
                            if attr.get('key') == 'id' and attr.get('value'):
                                # Value is a JSON string: {"owner":"...","dseq":"23989107"}
                                try:
                                    id_obj = json.loads(attr['value'])
                                    dseq_val = id_obj.get('dseq')
                                    if dseq_val:
                                        self.logger.debug(f"Parsed dseq from EventDeploymentCreated: {dseq_val}")
                                        return dseq_val
                                except Exception as e:
                                    self.logger.debug(f"Failed to parse dseq from id attribute: {e}")

                # 2. Try raw_log field for dseq
                if raw_log := output_data.get('raw_log', ''):
                    if match := re.search(r'"dseq":"(\d+)"', raw_log):
                        self.logger.debug(f"Parsed dseq from raw_log: {match.group(1)}")
                        return match.group(1)

                # 3. Fallback: scan for dseq in logs/events (legacy)
                for log in output_data.get('logs', []):
                    for event in log.get('events', []):
                        for attr in event.get('attributes', []):
                            if attr.get('key') == 'dseq' and attr.get('value'):
                                self.logger.debug(f"Parsed dseq from logs/events: {attr['value']}")
                                return attr['value']

                # 4. BAD: Do NOT use height as dseq
                if output_data.get('height'):
                    self.logger.warning(f"Height field present in output, but should NOT be used as dseq: {output_data['height']}")

        except (json.JSONDecodeError, Exception) as e:
            self.logger.debug(f"JSON parsing failed: {e}")

        self.logger.error("Failed to parse dseq from deployment output!")
        return None

    def _find_recent_deployment(self):
        """
        Query blockchain for the most recent active deployment for this wallet.
        Used as fallback when deployment creation times out or DSEQ can't be parsed.
        Returns DSEQ as string or None if no deployment found.
        """
        try:
            self.logger.debug("Querying blockchain for recent deployments...")
            success, result = self.execute_query(['query', 'deployment', 'list', '--owner', self.wallet_address])
            
            if not success or not isinstance(result, dict):
                self.logger.debug(f"Failed to query deployments: {result}")
                return None
            
            deployments = result.get('deployments', [])
            if not deployments:
                self.logger.debug("No deployments found on blockchain")
                return None
            
            # Find most recent active deployment
            active_deployments = []
            for dep_wrapper in deployments:
                deployment = dep_wrapper.get('deployment', {})
                state = deployment.get('state', 'unknown')
                
                # Check if deployment is active (newly created deployments are in 'active' state)
                if state == 'active':
                    dseq = deployment.get('deployment_id', {}).get('dseq')
                    if dseq:
                        active_deployments.append(str(dseq))
            
            if active_deployments:
                # Return the highest DSEQ (most recent)
                most_recent = max(active_deployments, key=lambda x: int(x))
                self.logger.debug(f"Found recent active deployment: DSEQ {most_recent}")
                return most_recent
            
            self.logger.debug("No active deployments found")
            return None
            
        except Exception as e:
            self.logger.warning(f"Error querying for recent deployment: {e}")
            return None

    def create_deployment(self):
        """Create deployment with resilient timeout handling"""
        self.logger.info("📦 Creating deployment...")
        if not self.ensure_act_for_deployment(required_uact=DEFAULT_DEPLOYMENT_DEPOSIT_UACT):
            return {'success': False, 'error': 'Insufficient ACT balance and failed to mint ACT for deployment deposit'}

        manifest_path = self.create_deployment_manifest(self.generate_api_credentials())
        success, stdout, stderr = self.execute_tx([
            'tx', 'deployment', 'create', manifest_path,
            '--deposit', f'{DEFAULT_DEPLOYMENT_DEPOSIT_UACT}uact'
        ])
        
        # Check if this is a timeout error (transaction might still succeed)
        is_timeout = 'timed out waiting for tx' in stderr.lower() or 'timeout' in stderr.lower()
        
        if not success:
            if is_timeout:
                self.logger.warning(f"⚠️  RPC timeout during deployment creation: {stderr}")
                self.logger.info("🔍 Checking blockchain for deployment creation despite timeout...")
                
                # Retry up to 3 times with 5-second waits
                max_retries = 3
                dseq = None
                
                for attempt in range(1, max_retries + 1):
                    self.logger.info(f"   Attempt {attempt}/{max_retries}: Waiting 5 seconds for blockchain propagation...")
                    time.sleep(5)
                    
                    # Query blockchain for recent deployments
                    dseq = self._find_recent_deployment()
                    
                    if dseq:
                        self.logger.info(f"✅ Deployment was created successfully despite timeout: DSEQ {dseq}")
                        self.get_wallet_balance()
                        deployment_info = {
                            'dseq': dseq,
                            'owner': self.wallet_address,
                            'manifest_path': manifest_path,
                            'act_conversion': self.last_act_conversion,
                            'wallet_balances': {'uakt': self.balance_uakt, 'uact': self.balance_uact}
                        }
                        self.save_state(deployment_info)
                        return {'success': True, 'deployment_info': deployment_info}
                    else:
                        if attempt < max_retries:
                            self.logger.debug(f"   No deployment found yet, retrying...")
                        else:
                            self.logger.error("❌ No deployment found on blockchain after 3 attempts")
                
                return {'success': False, 'error': f'Deployment creation timed out and no deployment found after {max_retries} attempts: {stderr}'}
            else:
                # Non-timeout error
                self.logger.error(f"❌ Deployment creation failed: {stderr}")
                return {'success': False, 'error': f'Deployment creation failed: {stderr}'}

        self.logger.debug(f"Deployment creation output: {stdout}")
        
        # Parse DSEQ from output
        dseq = self._parse_dseq_from_output(stdout)
        if not dseq:
            self.logger.warning(f"Could not parse DSEQ from output, checking blockchain...")
            dseq = self._find_recent_deployment()
            
            if not dseq:
                self.logger.error(f"Failed to parse DSEQ from output: {stdout}")
                return {'success': False, 'error': f'Failed to parse deployment output. Raw output: {stdout}'}

        self.logger.info(f"✅ Deployment created with DSEQ: {dseq}")
        self.get_wallet_balance()
        deployment_info = {
            'dseq': dseq,
            'owner': self.wallet_address,
            'manifest_path': manifest_path,
            'act_conversion': self.last_act_conversion,
            'wallet_balances': {'uakt': self.balance_uakt, 'uact': self.balance_uact}
        }
        self.save_state(deployment_info)
        return {'success': True, 'deployment_info': deployment_info}

    def wait_for_bids(self, dseq, timeout=300):
        """Wait for bids"""
        self.logger.info(f"⏳ Waiting for bids for deployment {dseq}...")
        
        # First, check deployment status to make sure it's open for bidding
        deploy_success, deploy_result = self.execute_query([
            'query', 'deployment', 'get', '--dseq', dseq, '--owner', self.wallet_address
        ])
        
        if deploy_success and isinstance(deploy_result, dict):
            deployment = deploy_result.get('deployment', {})
            state = deployment.get('state', 'unknown')
            self.logger.info(f"🔍 Deployment state: {state}")
            
            if state != 'active':
                self.logger.warning(f"⚠️  Deployment state is '{state}' - may not be accepting bids")
        else:
            self.logger.warning(f"⚠️  Could not check deployment status: {deploy_result}")
        
        start_time = time.time()
        bid_check_count = 0
        
        while time.time() - start_time < timeout:
            bid_check_count += 1
            
            # Use modular bid query method
            bid_result = self._query_bids(dseq, state_filter='open')
            
            self.logger.debug(f"Bid check #{bid_check_count} (RPC: {self.akash_node})")
            if self.debug_mode and bid_check_count <= 2:
                self.logger.debug(f"Bid query result: {bid_result}")
            
            if bid_result:
                open_bids = bid_result.get('open_bids', [])
                closed_bids = bid_result.get('closed_bids', [])
                
                if open_bids:
                    self.logger.info(f"✅ Received {len(open_bids)} open bids for DSEQ {dseq}")
                    return open_bids
                elif closed_bids:
                    self.logger.debug(f"Found {len(closed_bids)} closed bids - no open bids")
                else:
                    self.logger.debug(f"No open bids yet for DSEQ {dseq}")
            else:
                self.logger.debug(f"Bid query failed on {self.akash_node}")
            
            if bid_check_count % 6 == 0:  # Every minute (6 * 10s = 60s)
                elapsed = int(time.time() - start_time)
                self.logger.info(f"Still waiting for bids... ({elapsed}s elapsed, {bid_check_count} checks)")
            
            time.sleep(10)
        
        self.logger.warning(f"❌ No bids received within {timeout}s timeout")
        return None

    def select_best_bid(self, bids):
        """Select best bid using sophisticated scoring system"""
        if not bids:
            return None
        
        self.logger.info(f"🔍 Evaluating {len(bids)} bids using scoring system...")
        
        # Get GPU preferences once and log them
        gpu_preferences = self._get_gpu_preferences_from_manifest()
        self.logger.info(f"📋 GPU preferences from manifest: {gpu_preferences}")
        
        scored_bids = []
        for bid in bids:
            provider = bid['bid']['id']['provider']
            
            # Get provider attributes
            provider_attrs = self._get_provider_attributes(provider)
            if not provider_attrs:
                self.logger.warning(f"⚠️  Skipping bid from {provider[:20]}... - no attributes available")
                continue
            
            # Convert attributes to dict for easy access
            attr_dict = {}
            for attr in provider_attrs:
                key = attr.get('key', '')
                value = attr.get('value', '')
                attr_dict[key] = value
            
            # Extract GPU info using the helper method
            gpu_model = self._extract_gpu_model(provider_attrs)
            gpu_vendor = attr_dict.get('capabilities/gpu/vendor', 'unknown')
            country = attr_dict.get('country', 'Unknown')
            
            # Score the provider (pass gpu_preferences to avoid re-fetching)
            score = self._score_provider(provider, provider_attrs, gpu_preferences)
            price = int(float(bid['bid']['price']['amount']))
            
            # Combined score (higher is better, but factor in price)
            # Normalize price to score scale (lower price = higher score)
            max_reasonable_price = 5000  # uakt per block
            price_score = max(0, (max_reasonable_price - price) / max_reasonable_price * 100)
            
            # Weight: 70% provider quality, 30% price
            combined_score = (score * 0.7) + (price_score * 0.3)
            
            # Get provider URL from provider data
            provider_url = "N/A"
            try:
                success, provider_data = self.execute_query(['query', 'provider', 'get', provider, '--output', 'json'])
                if success and isinstance(provider_data, dict):
                    provider_url = provider_data.get('host_uri', 'N/A')
            except Exception:
                pass
            
            scored_bids.append({
                'bid': bid,
                'provider': provider,
                'provider_url': provider_url,
                'score': score,
                'price': price,
                'combined_score': combined_score,
                'attributes': provider_attrs,
                'gpu_model': gpu_model,
                'gpu_vendor': gpu_vendor,
                'country': country
            })
            
            # Log provider details with GPU info
            self.logger.info(f"  📊 {provider[:20]}... - GPU: {gpu_model} ({country}) - Score: {score:.1f}, Price: {price} uakt, Combined: {combined_score:.1f}")
        
        if not scored_bids:
            self.logger.error("❌ No valid bids after scoring")
            return None
        
        # Sort by combined score (highest first)
        scored_bids.sort(key=lambda x: x['combined_score'], reverse=True)
        
        best = scored_bids[0]
        self.logger.info(f"✅ Selected best bid: {best['provider']} (Score: {best['combined_score']:.1f})")
        self.logger.info(f"   GPU: {best['gpu_model']} | Location: {best['country']} | Price: {best['price']} uakt")
        self.logger.info(f"   Provider URL: {best['provider_url']}")
        
        return best['bid']

    def _get_provider_attributes(self, provider_address):
        """Get provider attributes from Akash network"""
        try:
            success, result = self.execute_query(['query', 'provider', 'get', provider_address, '--output', 'json'])
            if success and isinstance(result, dict):
                # The result is now directly the provider data in JSON format
                return result.get('attributes', [])
            return None
        except Exception as e:
            self.logger.warning(f"⚠️  Failed to get provider attributes for {provider_address[:20]}...: {e}")
            return None

    def _extract_gpu_model(self, attributes):
        """Extract GPU model from provider attributes.
        
        GPU models are stored as keys like:
        - capabilities/gpu/vendor/nvidia/model/a100
        - capabilities/gpu/vendor/nvidia/model/rtx3090
        """
        if not attributes:
            return 'Unknown'
        
        for attr in attributes:
            key = attr.get('key', '')
            # Look for keys matching the pattern: capabilities/gpu/vendor/nvidia/model/XXX
            if key.startswith('capabilities/gpu/vendor/nvidia/model/'):
                # Extract the model name from the key
                # Example: capabilities/gpu/vendor/nvidia/model/a100 -> a100
                parts = key.split('/')
                if len(parts) >= 6:
                    model = parts[5]  # The model name is at index 5
                    return model
        
        return 'Unknown'

    def _score_provider(self, provider_address, attributes, gpu_preferences=None):
        """Score provider based on attributes and GPU preferences"""
        if not attributes:
            return 0
        
        score = 0
        attr_dict = {}
        
        # Convert attributes list to dict
        for attr in attributes:
            key = attr.get('key', '')
            value = attr.get('value', '')
            attr_dict[key] = value
        
        # Location scoring (US preference)
        country = attr_dict.get('country', '').upper()
        region = attr_dict.get('region', '').lower()
        is_us_based = (country == 'US') or ('us-' in region)
        
        if is_us_based:
            score += 50
        elif country in ['CA', 'GB', 'DE', 'NL', 'AU']:
            score += 30
        
        # GPU scoring based on manifest preferences
        if gpu_preferences is None:
            gpu_preferences = self._get_gpu_preferences_from_manifest()
        
        # Extract GPU model from attributes
        gpu_model = self._extract_gpu_model(attributes).lower()
        
        if gpu_preferences and gpu_model and gpu_model != 'unknown':
            for i, preferred_gpu in enumerate(gpu_preferences):
                if preferred_gpu.lower() in gpu_model:
                    # Higher score for higher priority GPUs (larger gap to ensure GPU preference dominates)
                    score += 100 - (i * 30)
                    break
        elif attr_dict.get('capabilities/gpu/vendor') == 'nvidia':
            score += 25  # Basic NVIDIA support
        
        # Organization quality (minor bonus, should not override GPU preferences)
        organization = attr_dict.get('organization', '').lower()
        if 'overclock' in organization:
            score += 10
        elif 'datacenter' in attr_dict.get('location-type', '').lower():
            score += 5
        
        return score

    def _get_gpu_preferences_from_manifest(self):
        """Extract GPU preferences from manifest (does not log - caller should log)"""
        try:
            manifest = None
            if self.yaml_file:
                with open(self.yaml_file, 'r') as f:
                    manifest = yaml.safe_load(strip_cli_warnings(f.read()))
            elif self.yaml_content:
                manifest = yaml.safe_load(strip_cli_warnings(self.yaml_content))
            else:
                # Default preferences if no manifest
                return ['rtx4090', 'a100', 'h100', 'rtx3090', 'rtx3080']
            
            # Extract GPU preferences from profiles.compute.*.resources.gpu.attributes.vendor.nvidia
            profiles = manifest.get('profiles', {})
            compute = profiles.get('compute', {})
            
            gpu_preferences = []
            for profile_name, profile_config in compute.items():
                resources = profile_config.get('resources', {})
                gpu = resources.get('gpu', {})
                attributes = gpu.get('attributes', {})
                vendor = attributes.get('vendor', {})
                nvidia = vendor.get('nvidia', [])
                
                # Extract models in order of preference
                for gpu_spec in nvidia:
                    if isinstance(gpu_spec, dict) and 'model' in gpu_spec:
                        model = gpu_spec['model'].lower()
                        if model not in gpu_preferences:
                            gpu_preferences.append(model)
                
                # If we found GPU preferences, return them (no logging - let caller log)
                if gpu_preferences:
                    return gpu_preferences
            
            # Default GPU preference order if not specified in manifest
            default_prefs = ['rtx4090', 'a100', 'h100', 'rtx3090', 'rtx3080', 'v100', 'a6000']
            return default_prefs
            
        except Exception as e:
            self.logger.warning(f"⚠️  Could not extract GPU preferences: {e}")
            default_prefs = ['rtx4090', 'a100', 'h100']
            return default_prefs

    def create_lease(self, dseq, bid):
        """Create lease and save provider info"""
        provider = bid['bid']['id']['provider']
        gseq = str(bid['bid']['id']['gseq'])
        oseq = str(bid['bid']['id']['oseq'])
        
        self.logger.info(f"🤝 Creating lease with provider {provider}")
        
        success, stdout, stderr = self.execute_tx([
            'tx', 'market', 'lease', 'create', 
            '--dseq', str(dseq), '--gseq', gseq, '--oseq', oseq, '--provider', provider
        ])
        
        if success:
            lease_info = {
                'provider': provider,
                'dseq': dseq,
                'gseq': gseq,
                'oseq': oseq,
                'status': 'active'
            }
            
            # Update deployment state with provider info
            deployment_state = self.load_state()
            if deployment_state:
                deployment_state.update(lease_info)
                self.save_state(deployment_state)
            
            self.logger.info(f"✅ Lease created successfully")
            return {'success': True, 'lease_info': lease_info}
        else:
            self.logger.error(f"❌ Lease creation failed: {stderr}")
            return {'success': False, 'error': f'Lease creation failed: {stderr}'}

    def send_manifest(self, manifest_file, dseq):
        """Send manifest to provider"""
        # Get provider info from saved state
        deployment_state = self.load_state()
        if not deployment_state or 'provider' not in deployment_state:
            return {'success': False, 'error': 'No provider information found in deployment state'}
        
        provider = deployment_state['provider']
        gseq = deployment_state.get('gseq', '1')
        oseq = deployment_state.get('oseq', '1')
        
        self.logger.info(f"📤 Sending manifest to provider {provider[:20]}...")
        
        # Use send-manifest command directly (not a tx command, communicates directly with provider)
        cmd = [
            'provider-services', 'send-manifest', manifest_file,
            '--dseq', str(dseq), '--gseq', gseq, '--oseq', oseq, '--provider', provider,
            '--keyring-backend', AKASH_KEYRING_BACKEND, '--from', AKASH_WALLET_NAME,
            '--node', self.akash_node, '--auth-type', 'mtls'
        ]
        
        stdout, stderr, returncode = self.run_command(cmd, timeout=60)
        success = returncode == 0
        
        if success:
            self.logger.info(f"✅ Manifest sent successfully to provider")
            return {'success': True, 'message': 'Manifest sent successfully'}
        else:
            self.logger.error(f"❌ Manifest send failed: {stderr}")
            return {'success': False, 'error': f'Manifest send failed: {stderr}'}

    def check_service_status(self, dseq):
        """Check service status and readiness"""
        # Get provider info from saved state
        deployment_state = self.load_state()
        if not deployment_state or 'provider' not in deployment_state:
            # Try to fetch provider info from blockchain
            self.logger.info(f"⚠️  Provider info not in state file, querying blockchain for lease...")
            lease_info = self._get_lease_info_for_deployment(str(dseq))
            
            if not lease_info or 'provider' not in lease_info:
                return {'success': False, 'error': 'No provider information found in deployment state and no active lease found on blockchain. Deployment may not have been fully created or lease was not established.'}
            
            # Update state with lease info
            self.logger.info(f"✅ Found lease info from blockchain, updating state file")
            if deployment_state:
                deployment_state.update(lease_info)
                self.save_state(deployment_state)
            
            provider = lease_info['provider']
            gseq = lease_info.get('gseq', '1')
            oseq = lease_info.get('oseq', '1')
        else:
            provider = deployment_state['provider']
            gseq = deployment_state.get('gseq', '1')
            oseq = deployment_state.get('oseq', '1')
        
        self.logger.info(f"🔍 Checking service status for deployment {dseq}")
        
        # Check lease status - use direct command, not query
        cmd = [
            'provider-services', 'lease-status',
            '--dseq', str(dseq), '--gseq', gseq, '--oseq', oseq, '--provider', provider,
            '--keyring-backend', AKASH_KEYRING_BACKEND, '--from', AKASH_WALLET_NAME,
            '--node', self.akash_node, '--auth-type', 'mtls'
        ]
        
        stdout, stderr, returncode = self.run_command(cmd, timeout=30)
        success = returncode == 0
        
        if success:
            try:
                # Try JSON first, then YAML as fallback
                try:
                    status_data = json.loads(strip_cli_warnings(stdout)) if stdout else {}
                except json.JSONDecodeError:
                    status_data = yaml.safe_load(strip_cli_warnings(stdout)) if stdout else {}
                
                services = status_data.get('services', {})
                
                # Check if all services are running
                all_ready = True
                service_info = []
                
                for service_name, service_data in services.items():
                    # Use ready_replicas and available_replicas from JSON output
                    ready = service_data.get('ready_replicas', service_data.get('ready', 0))
                    available = service_data.get('available_replicas', service_data.get('available', service_data.get('total', 0)))
                    
                    service_info.append({
                        'name': service_name,
                        'available': available,
                        'ready': ready,
                        'status': 'ready' if ready > 0 else 'starting'
                    })
                    
                    if ready == 0:
                        all_ready = False
                
                status = 'ready' if all_ready and service_info else 'starting'
                
                self.logger.info(f"Service status: {status}")
                for svc in service_info:
                    self.logger.info(f"  - {svc['name']}: {svc['ready']}/{svc['available']} ready")
                
                # Extract URIs from services
                service_uris = {}
                for service_name, service_data in services.items():
                    uris = service_data.get('uris', [])
                    if uris:
                        service_uris[service_name] = uris
                
                return {
                    'success': True, 
                    'status': status,
                    'services': service_info,
                    'all_ready': all_ready,
                    'service_uris': service_uris
                }
                
            except Exception as e:
                self.logger.warning(f"Failed to parse service status: {e}")
                return {'success': True, 'status': 'unknown', 'raw_output': stdout}
        else:
            self.logger.error(f"❌ Service status check failed: {stderr}")
            return {'success': False, 'error': f'Service status check failed: {stderr}'}

    def check_models_downloaded(self, dseq):
        """Check logs for model download completion indicator"""
        logs_result = self.get_lease_logs(tail_lines=200)
        
        if logs_result.get('success') and logs_result.get('logs'):
            logs = logs_result['logs']
            # Look for the indicator that watchers have been established, meaning models are downloaded
            if 'Watches established' in logs or 'watchers started' in logs.lower():
                self.logger.info("✅ Models downloaded and watchers established")
                return True
            elif 'Downloads complete' in logs:
                self.logger.info("⏳ Downloads complete, waiting for watchers...")
                return False
            else:
                self.logger.debug("Still downloading models...")
                return False
        
        return False

    def wait_for_ready(self, dseq, provider, timeout=900):
        """Wait for deployment ready"""
        self.logger.info("⏳ Waiting for deployment to become ready...")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            # Check service status using our enhanced method
            status_result = self.check_service_status(dseq)
            
            if status_result['success']:
                if status_result.get('all_ready', False):
                    # Services are ready, check if models are downloaded
                    if self.check_models_downloaded(dseq):
                        self.logger.info("✅ Deployment is fully ready (services + models)!")
                        
                        # Get the service URL from URIs
                        service_uris = status_result.get('service_uris', {})
                        if service_uris:
                            # Get the first service's first URI (usually comfyui)
                            for service_name, uris in service_uris.items():
                                if uris and len(uris) > 0:
                                    # Construct full URL with https
                                    service_url = f"https://{uris[0]}"
                                    self.logger.info(f"🌐 Service URL: {service_url}")
                                    return service_url
                        
                        self.logger.warning("⚠️ Services ready but no URIs found")
                        return None
                    else:
                        self.logger.info("⏳ Services ready, waiting for model downloads...")
                else:
                    # Services are still starting
                    self.logger.info(f"Services starting... ({status_result.get('status', 'unknown')})")
            else:
                self.logger.warning(f"Service status check failed: {status_result.get('error', 'Unknown error')}")
            
            time.sleep(30)
        
        self.logger.error(f"❌ Deployment failed to become ready within {timeout} seconds")
        return None

    def _update_deployment_metadata(self, deployment_info, dseq):
        """Update deployment metadata (service_url and api_credentials) if missing"""
        # Get service URL if not already in state
        service_url = deployment_info.get('service_url', '')
        if not service_url:
            service_url = self.get_service_url_from_lease(dseq, deployment_info)
            if service_url:
                deployment_info['service_url'] = service_url
                self.save_state(deployment_info)
        
        # Get or generate API credentials if not already in state
        api_credentials = deployment_info.get('api_credentials', {})
        if not api_credentials:
            api_credentials = self.generate_api_credentials(service_url)
            deployment_info['api_credentials'] = api_credentials
            self.save_state(deployment_info)
        elif api_credentials.get('api_url') == 'http://service-url-placeholder' and service_url:
            # Update placeholder with actual URL
            api_credentials['api_url'] = service_url
            deployment_info['api_credentials'] = api_credentials
            self.save_state(deployment_info)
        
        return service_url, api_credentials

    def get_service_url_from_lease(self, dseq, deployment_info=None):
        """Get service URL from lease status
        
        Args:
            dseq: Deployment sequence number
            deployment_info: Optional deployment info dict containing provider details.
                           If not provided, will load from state file.
        """
        # Save deployment_info temporarily if provided (so check_service_status can use it)
        temp_state_saved = False
        if deployment_info and deployment_info.get('provider'):
            self.save_state(deployment_info)
            temp_state_saved = True
        
        status_result = self.check_service_status(dseq)
        if status_result.get('success'):
            service_uris = status_result.get('service_uris', {})
            for service_name, uris in service_uris.items():
                if uris and len(uris) > 0:
                    return f"https://{uris[0]}"
        return ""
    
    def get_active_deployment_info(self):
        """Get active deployment info"""
        deployment_info = self.load_state()
        if deployment_info and deployment_info.get('dseq'):
            return deployment_info['dseq'], deployment_info.get('provider', '')
        
        # Query for active deployments
        if not self.wallet_address:
            return None, None
        
        success, result = self.execute_query(['query', 'deployment', 'list'])
        if success and isinstance(result, dict):
            deployments = result.get('deployments', [])
            for deployment in deployments:
                if deployment.get('deployment', {}).get('deployment_id', {}).get('owner') == self.wallet_address:
                    dseq = deployment.get('deployment', {}).get('deployment_id', {}).get('dseq')
                    if dseq:
                        return str(dseq), ""
        return None, None

    def check_ready(self):
        """Check if deployment is ready (services running + models downloaded)"""
        try:
            # Use helper to restore wallet and get deployment
            success, deployment_info, error_response = self._ensure_wallet_and_deployment()
            if not success:
                if error_response:
                    error_response['ready'] = False
                    return error_response
                return {'success': False, 'error': 'Unknown error', 'ready': False}
            
            dseq = deployment_info.get('dseq') if deployment_info else None
            self.logger.info(f"🔍 Checking if deployment {dseq} is ready...")
            
            # Switch to dseq-specific log file
            self._switch_to_dseq_log_file(dseq)
            
            # Check service status
            status_result = self.check_service_status(dseq)
            
            if not status_result.get('success'):
                error_msg = status_result.get('error', 'Unknown error')
                self.logger.error(f"❌ Service status check failed: {error_msg}")
                return {
                    'success': True,
                    'ready': False,
                    'status': 'error_checking_status',
                    'error': f'Failed to check service status: {error_msg}',
                    'dseq': dseq
                }

            # Update service_url/api_url as soon as provider exposes URIs,
            # even if models are still downloading
            service_uris = status_result.get('service_uris', {})
            service_url = None
            for _, uris in service_uris.items():
                if uris and len(uris) > 0:
                    service_url = f"https://{uris[0]}"
                    break

            if service_url and deployment_info:
                updated = False
                if deployment_info.get('service_url') != service_url:
                    deployment_info['service_url'] = service_url
                    updated = True

                api_credentials = deployment_info.get('api_credentials', {})
                if not api_credentials:
                    api_credentials = self.generate_api_credentials(service_url)
                    deployment_info['api_credentials'] = api_credentials
                    updated = True
                elif api_credentials.get('api_url') == 'http://service-url-placeholder' or not api_credentials.get('api_url'):
                    api_credentials['api_url'] = service_url
                    deployment_info['api_credentials'] = api_credentials
                    updated = True

                if updated:
                    self.save_state(deployment_info)
                    self.logger.info(f"🔄 Updated deployment metadata early with service URL: {service_url}")
            
            all_ready = status_result.get('all_ready', False)
            
            # If services are not ready, return early
            if not all_ready:
                self.logger.info(f"⏳ Services still starting...")
                return {
                    'success': True,
                    'ready': False,
                    'dseq': dseq,
                    'status': 'starting_services',
                    'message': 'Services are still starting'
                }
            
            # Services are ready, check models
            self.logger.info(f"✅ Services are ready, checking model downloads...")
            models_ready = self.check_models_downloaded(dseq)
            
            if not models_ready:
                self.logger.info(f"⏳ Services ready, waiting for model downloads...")
                return {
                    'success': True,
                    'ready': False,
                    'dseq': dseq,
                    'status': 'downloading_models',
                    'message': 'Services are ready, models still downloading'
                }
            
            # Everything is ready! Update metadata
            self.logger.info(f"✅ Models downloaded, finalizing deployment...")
            service_url, api_credentials = self._update_deployment_metadata(deployment_info, dseq)
            
            # Update state to 'ready'
            if deployment_info:
                deployment_info['status'] = 'ready'
                self.save_state(deployment_info)
            
            self.logger.info(f"✅ Deployment {dseq} is fully ready!")
            
            return {
                'success': True,
                'ready': True,
                'dseq': dseq,
                'deployment_info': deployment_info,
                'service_url': service_url,
                'api_credentials': api_credentials,
                'status': 'ready',
                'message': 'Deployment is fully ready'
            }
        
        except Exception as e:
            self.logger.error(f"❌ Check ready failed: {e}")
            return {
                'success': False,
                'error': str(e),
                'ready': False,
                'traceback': traceback.format_exc()
            }

    def run(self):
        """Main deployment workflow"""
        try:
            # IMPORTANT: Restore wallet FIRST so we have wallet_address for checking existing deployments
            if not self.restore_wallet():
                return self._error_response('Wallet restoration failed')

            # Now check for existing active deployment (wallet_address is set)
            has_active, active_deployment_info = self.has_active_deployment()
            if has_active and active_deployment_info:
                # We have a verified active deployment
                dseq = active_deployment_info.get('dseq')
                self.logger.info(f"✅ Found existing active deployment: DSEQ {dseq}")
                
                # Switch to dseq-specific log file
                self._switch_to_dseq_log_file(dseq)
                
                # Check if deployment has lease info (provider, gseq, oseq)
                has_lease_info = active_deployment_info.get('provider') and active_deployment_info.get('gseq') and active_deployment_info.get('oseq')
                
                if not has_lease_info:
                    # Deployment exists but no lease - check if lease exists on blockchain
                    self.logger.warning(f"⚠️  Deployment {dseq} has no lease info in state, checking blockchain...")
                    lease_info = self._get_lease_info_for_deployment(str(dseq))
                    
                    if lease_info and lease_info.get('provider'):
                        # Lease exists on blockchain, update state
                        self.logger.info(f"✅ Found lease on blockchain, updating state")
                        active_deployment_info.update(lease_info)
                        self.save_state(active_deployment_info)
                    else:
                        # No lease on blockchain - check bid status
                        self.logger.warning(f"⚠️  No lease found for deployment {dseq}, checking bid status...")
                        
                        # Check deployment age
                        deployment_age_minutes = 0
                        state = self.load_state()
                        if state:
                            created_at = state.get('created_at', '')
                            if created_at:
                                try:
                                    created_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                                    deployment_age_minutes = (datetime.now(timezone.utc) - created_time).total_seconds() / 60
                                except Exception as e:
                                    self.logger.debug(f"Could not parse created_at time: {e}")
                        
                        # Query for all bids (open and closed)
                        bid_result = self._query_bids(dseq, state_filter='all')
                        
                        if not bid_result:
                            # Failed to query bids
                            self.logger.error(f"❌ Failed to query bids for deployment {dseq}")
                            return self._error_response('Could not query bid status for existing deployment')
                        
                        open_bids = bid_result.get('open_bids', [])
                        closed_bids = bid_result.get('closed_bids', [])
                        all_bids = bid_result.get('all_bids', [])
                        
                        self.logger.info(f"📊 Bid status: {len(open_bids)} open, {len(closed_bids)} closed, {len(all_bids)} total")
                        
                        if open_bids:
                            # We have open bids - try to create lease
                            self.logger.info(f"✅ Found {len(open_bids)} open bid(s), attempting to create lease...")
                            best_bid = self.select_best_bid(open_bids)
                            lease_result = self.create_lease(dseq, best_bid)
                            
                            if lease_result['success']:
                                self.logger.info(f"✅ Lease created successfully!")
                                # Update state with lease info
                                active_deployment_info.update(lease_result['lease_info'])
                                self.save_state(active_deployment_info)
                                
                                # Send manifest
                                manifest_path = active_deployment_info.get('manifest_path')
                                if manifest_path:
                                    manifest_result = self.send_manifest(manifest_path, dseq)
                                    if not manifest_result['success']:
                                        self.logger.warning(f"⚠️  Manifest send failed: {manifest_result.get('error')}")
                                else:
                                    self.logger.warning(f"⚠️  No manifest path in state, cannot send manifest")
                            else:
                                self.logger.error(f"❌ Failed to create lease: {lease_result.get('error')}")
                                return self._error_response(f"Failed to create lease for existing deployment: {lease_result.get('error')}")
                        
                        elif closed_bids and not open_bids:
                            # Had bids but they all closed/expired - close deployment
                            self.logger.warning(f"⚠️  Deployment had {len(closed_bids)} bid(s) but all expired. Closing deployment...")
                            close_result = self.close_deployment(dseq)
                            if close_result.get('success'):
                                self.logger.info(f"✅ Closed deployment {dseq} with expired bids")
                                return self._error_response('Previous deployment had bids but they all expired. Please create a new deployment.')
                            else:
                                return self._error_response(f"Deployment has expired bids and failed to close: {close_result.get('error')}")
                        
                        elif not all_bids and deployment_age_minutes > 5:
                            # Never had any bids and deployment is old (>5 min) - close it
                            self.logger.warning(f"⚠️  No bids received after {deployment_age_minutes:.1f} minutes. Closing stale deployment...")
                            close_result = self.close_deployment(dseq)
                            if close_result.get('success'):
                                self.logger.info(f"✅ Closed stale deployment {dseq}")
                                return self._error_response('Previous deployment expired (no bids received). Please create a new deployment.')
                            else:
                                return self._error_response(f"Deployment is stale and failed to close: {close_result.get('error')}")
                        
                        else:
                            # No bids yet but deployment is young (<5 min) - wait
                            self.logger.info(f"⏳ Deployment is {deployment_age_minutes:.1f} minutes old, waiting for bids...")
                            return self._error_response(f'Deployment exists but has no bids yet (age: {deployment_age_minutes:.1f} min). Wait for bids or close deployment.')
                
                # At this point we have a deployment with lease info
                self.logger.info(f"✅ Using existing deployment with lease: DSEQ {dseq}")
                
                # Update metadata (service_url and api_credentials)
                service_url, api_credentials = self._update_deployment_metadata(active_deployment_info, dseq)
                
                return {
                    'success': True,
                    'deployment_info': active_deployment_info,
                    'api_credentials': api_credentials,
                    'service_url': service_url,
                    'message': f"Using existing active deployment: DSEQ {dseq}"
                }

            # No existing deployment found, continue with checks for new deployment
            if self.get_wallet_balance() < 1000000:
                return self._error_response('Insufficient balance')

            if not self.setup_certificate():
                return self._error_response('Certificate setup failed')

            # Create deployment
            result = self.create_deployment()
            if not result['success']:
                return result

            dseq = result['deployment_info']['dseq']
            
            # Switch to dseq-specific log file now that we have the dseq
            self._switch_to_dseq_log_file(dseq)
            
            # Wait for bids
            bids = self.wait_for_bids(dseq)
            if not bids:
                return self._error_response('No bids received', deployment_info=result.get('deployment_info'))

            # Create lease
            best_bid = self.select_best_bid(bids)
            lease_result = self.create_lease(dseq, best_bid)
            if not lease_result['success']:
                return lease_result

            provider = lease_result['lease_info']['provider']
            
            # Send manifest and wait
            manifest_path = result['deployment_info'].get('manifest_path')
            if not manifest_path:
                return self._error_response('Manifest path not found in deployment info', 
                                           deployment_info=result.get('deployment_info'),
                                           lease_info=lease_result.get('lease_info'))
            
            manifest_result = self.send_manifest(manifest_path, dseq)
            if not manifest_result['success']:
                return self._error_response(f"Manifest send failed: {manifest_result.get('error', 'Unknown error')}",
                                           deployment_info=result.get('deployment_info'),
                                           lease_info=lease_result.get('lease_info'))

            # NEW DEFAULT BEHAVIOR: Return immediately after manifest send
            # The deployment will continue starting in the background
            # Use --check-ready to poll for readiness
            self.logger.info("✅ Deployment started successfully (manifest sent)")
            self.logger.info("⏳ Services are starting in the background...")
            self.logger.info("💡 Use --check-ready to check when deployment is fully ready")
            
            # Generate placeholder credentials (will be updated when ready)
            api_credentials = self.generate_api_credentials()

            # Try to get service URL early (may or may not be available yet)
            early_service_url = self.get_service_url_from_lease(dseq)
            if early_service_url:
                api_credentials['api_url'] = early_service_url
            
            # Save to state with 'starting' status
            final_deployment_state = self.load_state()
            if final_deployment_state:
                final_deployment_state['api_credentials'] = api_credentials
                if early_service_url:
                    final_deployment_state['service_url'] = early_service_url
                final_deployment_state['status'] = 'starting'
                self.save_state(final_deployment_state)
            
            # Send deployment started notification email
            try:
                self.get_wallet_balance()
                act_conversion = result.get('deployment_info', {}).get('act_conversion', {}) or {}
                if act_conversion.get('conversion_performed'):
                    conversion_info = f"""ACT Conversion:
- Status: Performed
- Burned: {act_conversion.get('burned_akt', 0):.6f} AKT ({act_conversion.get('burned_uakt', 0)} uakt)
- ACT Before: {act_conversion.get('act_balance_before_act', 0):.6f} ACT
- ACT After: {act_conversion.get('act_balance_after_act', 0):.6f} ACT
- ACT Minted (est): {act_conversion.get('minted_act_estimate', 0):.6f} ACT
- ACT Minted (ledger): {act_conversion.get('minted_uact_from_ledger', 0) / 1000000:.6f} ACT
- ACT Minted (bank delta): {act_conversion.get('minted_uact_from_balance', 0) / 1000000:.6f} ACT
- ACT Effective Balance: {act_conversion.get('effective_act_balance_act', act_conversion.get('act_balance_after_act', 0)):.6f} ACT
- Ledger Status: {act_conversion.get('ledger_status', 'unknown')}"""
                else:
                    conversion_info = f"""ACT Conversion:
- Status: Not needed
- Details: {act_conversion.get('message', 'No conversion metadata')}"""

                subject = f"Akash Deployment {dseq} Started"
                body = f"""ComfyUI Deployment Started

DSEQ: {dseq}
Provider: {provider}
Status: Starting (services launching in background)
Time: {datetime.now(timezone.utc).isoformat()}Z
Service URL: {early_service_url if early_service_url else 'Pending - use --check-ready to get URL when available'}

API Credentials:
- Username: {api_credentials['username']}
- Password: {api_credentials['password']}
- API URL: {api_credentials['api_url']}

{conversion_info}

Wallet Balances (post-deployment-start):
- AKT: {self.balance_uakt / 1000000:.6f} AKT ({self.balance_uakt} uakt)
- ACT: {self.balance_uact / 1000000:.6f} ACT ({self.balance_uact} uact)

The deployment is starting. Services will be available once fully initialized.
Use --check-ready to monitor deployment status.
"""
                self.send_email(subject, body)
            except Exception as e:
                self.logger.warning(f"⚠️ Could not send deployment notification: {e}")
            
            return {
                'success': True,
                'message': 'Deployment started successfully. Use --check-ready to verify when ready.',
                'deployment_info': result['deployment_info'],
                'lease_info': lease_result['lease_info'],
                'service_url': None,  # Not available yet
                'api_credentials': api_credentials,
                'ready': False,
                'status': 'starting',
                'dseq': dseq,
                'provider': provider
            }


        except Exception as e:
            self.logger.error(f"❌ Deployment failed: {e}")
            return self._error_response(f'Deployment failed with exception: {str(e)}')

    def close_deployment(self, dseq=None):
        """Close deployment"""
        try:
            deployment_state_before_close = self.load_state() or {}
            if not dseq:
                # Use helper to restore wallet and get deployment
                success, deployment_info, error_response = self._ensure_wallet_and_deployment()
                if not success:
                    return error_response if error_response else {'success': False, 'error': 'Unknown error'}
                
                dseq = deployment_info.get('dseq') if deployment_info else None

            self.logger.info(f"🛑 Closing deployment {dseq}...")
            
            # Close the deployment first
            success, stdout, _ = self.execute_tx(['tx', 'deployment', 'close', '--dseq', dseq])
            
            if success:
                self.clear_state()
                
                # Extract transaction fee from close transaction
                tx_fee_akt = 0.0
                lease_cost_akt = 0.0
                
                try:
                    if stdout:
                        tx_data = json.loads(strip_cli_warnings(stdout))
                        if tx_data and isinstance(tx_data, dict):
                            fee_info = tx_data.get('tx', {}).get('auth_info', {}).get('fee', {})
                            for amount in fee_info.get('amount', []):
                                if amount.get('denom') == 'uakt':
                                    tx_fee_akt = float(amount['amount']) / 1000000
                                    break
                except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
                    pass
                
                # Wait for blockchain confirmation then query actual lease cost
                self.logger.info("� Waiting for blockchain confirmation...")
                time.sleep(3)
                
                self.logger.info("🔍 Querying lease for actual cost...")
                try:
                    success_query, result = self.execute_query([
                        'query', 'market', 'lease', 'list', '--owner', self.wallet_address, '--dseq', dseq
                    ])
                    if success_query and isinstance(result, dict):
                        leases = result.get('leases', [])
                        if leases:
                            escrow = leases[0].get('escrow_payment', {})
                            withdrawn = escrow.get('withdrawn', {})
                            if isinstance(withdrawn, dict):
                                withdrawn_uakt = float(withdrawn.get('amount', 0))
                            else:
                                withdrawn_uakt = float(withdrawn) if withdrawn else 0
                            lease_cost_akt = withdrawn_uakt / 1000000
                            self.logger.info(f"💰 Lease cost: {lease_cost_akt:.6f} AKT")
                        else:
                            self.logger.warning("⚠️ No lease information found")
                except Exception as e:
                    self.logger.warning(f"⚠️ Could not query lease cost: {e}")
                
                # Calculate total cost
                total_cost_akt = lease_cost_akt + tx_fee_akt
                
                # Get AKT price for USD conversion
                akt_price = self.get_akt_price()
                if akt_price:
                    lease_cost_usd = lease_cost_akt * akt_price
                    tx_fee_usd = tx_fee_akt * akt_price
                    total_cost_usd = total_cost_akt * akt_price
                    usd_info = f"""- Lease Cost: ${lease_cost_usd:.2f} USD
- Transaction Fee: ${tx_fee_usd:.2f} USD
- Total Cost: ${total_cost_usd:.2f} USD
- AKT/USD Rate: ${akt_price:.2f}"""
                else:
                    usd_info = "- USD conversion: Not available (API unavailable)"
                
                # Send closure notification
                try:
                    self.get_wallet_balance()
                    act_conversion = deployment_state_before_close.get('act_conversion', {})
                    if act_conversion.get('conversion_performed'):
                        conversion_info = f"""ACT Conversion (from deployment start):
- Status: Performed
- Burned: {act_conversion.get('burned_akt', 0):.6f} AKT ({act_conversion.get('burned_uakt', 0)} uakt)
- ACT Before: {act_conversion.get('act_balance_before_act', 0):.6f} ACT
- ACT After: {act_conversion.get('act_balance_after_act', 0):.6f} ACT
- ACT Minted (est): {act_conversion.get('minted_act_estimate', 0):.6f} ACT
- ACT Minted (ledger): {act_conversion.get('minted_uact_from_ledger', 0) / 1000000:.6f} ACT
- ACT Minted (bank delta): {act_conversion.get('minted_uact_from_balance', 0) / 1000000:.6f} ACT
- ACT Effective Balance: {act_conversion.get('effective_act_balance_act', act_conversion.get('act_balance_after_act', 0)):.6f} ACT
- Ledger Status: {act_conversion.get('ledger_status', 'unknown')}"""
                    else:
                        conversion_info = f"""ACT Conversion (from deployment start):
- Status: Not recorded or not needed
- Details: {act_conversion.get('message', 'No conversion metadata found')}"""

                    subject = f"Akash Deployment {dseq} Closed - Cost Report"
                    body = f"""Deployment Closure Report

DSEQ: {dseq}
Closed: {datetime.now(timezone.utc).isoformat()}Z

Cost Analysis:
- Lease Cost: {lease_cost_akt:.6f} AKT
- Transaction Fee: {tx_fee_akt:.6f} AKT
- Total Cost: {total_cost_akt:.6f} AKT

{usd_info}

{conversion_info}

Wallet Balances (post-close):
- AKT: {self.balance_uakt / 1000000:.6f} AKT ({self.balance_uakt} uakt)
- ACT: {self.balance_uact / 1000000:.6f} ACT ({self.balance_uact} uact)

Deployment closed and wallet cleaned up.
"""
                    self.send_email(subject, body)
                except Exception as e:
                    self.logger.warning(f"⚠️ Could not send closure notification: {e}")
                
                return {'success': True, 'message': f'Deployment {dseq} closed', 'dseq': dseq}
            return {'success': False, 'error': 'Deployment closure failed'}
        
        finally:
            # Always clean up wallet after closing deployment for security
            self.cleanup_wallet()

    def get_lease_status(self):
        """Get lease status"""
        # Use helper to restore wallet and get deployment
        success, deployment_info, error_response = self._ensure_wallet_and_deployment()
        if not success:
            return error_response if error_response else {'success': False, 'error': 'Unknown error'}
        
        dseq = deployment_info.get('dseq') if deployment_info else None
        provider = deployment_info.get('provider', '') if deployment_info else ''
        
        if not provider:
            return {'success': False, 'error': 'Provider not found'}

        # Use check_service_status which properly calls lease-status
        status_result = self.check_service_status(dseq)
        return {
            'success': status_result.get('success', False),
            'dseq': dseq,
            'provider': provider,
            'status': status_result.get('status'),
            'services': status_result.get('services', []),
            'all_ready': status_result.get('all_ready', False)
        }

    def get_lease_logs(self, follow=False, tail_lines=100):
        """Get lease logs"""
        # Use helper to restore wallet and get deployment
        success, deployment_info, error_response = self._ensure_wallet_and_deployment()
        if not success:
            return error_response if error_response else {'success': False, 'error': 'Unknown error'}
        
        dseq = deployment_info.get('dseq') if deployment_info else None
        provider = deployment_info.get('provider', '') if deployment_info else ''
        gseq = deployment_info.get('gseq', '1') if deployment_info else '1'
        oseq = deployment_info.get('oseq', '1') if deployment_info else '1'
        
        if not dseq or not provider:
            return {'success': False, 'error': 'No active deployment found'}

        cmd = [
            'provider-services', 'lease-logs',
            '--dseq', str(dseq), '--gseq', gseq, '--oseq', oseq,
            '--provider', provider,
            '--keyring-backend', AKASH_KEYRING_BACKEND, '--from', AKASH_WALLET_NAME,
            '--node', self.akash_node, '--auth-type', 'mtls'
        ]
        
        if follow:
            cmd.append('-f')
        else:
            cmd.extend(['--tail', str(tail_lines)])

        stdout, stderr, rc = self.run_command(cmd, timeout=30)
        return {'success': rc == 0, 'dseq': dseq, 'provider': provider, 'logs': stdout if rc == 0 else stderr}

    def get_interactive_shell(self, service_name='comfyui'):
        """Get interactive shell into the container"""
        # Use helper to restore wallet and get deployment
        success, deployment_info, error_response = self._ensure_wallet_and_deployment()
        if not success:
            return error_response if error_response else {'success': False, 'error': 'Unknown error'}

        dseq = deployment_info.get('dseq') if deployment_info else None
        provider = deployment_info.get('provider') if deployment_info else None
        gseq = deployment_info.get('gseq', '1') if deployment_info else '1'
        oseq = deployment_info.get('oseq', '1') if deployment_info else '1'

        if not dseq or not provider:
            return {'success': False, 'error': 'Missing deployment info'}

        self.logger.info(f"🐚 Opening interactive shell for deployment {dseq}")
        self.logger.info(f"   Service: {service_name}")
        self.logger.info(f"   Provider: {provider}")
        self.logger.info(f"   Type 'exit' to close the shell\n")
        
        # Use os.execvp to replace the current process with the shell command
        # This provides a true interactive experience
        cmd = [
            'provider-services', 'lease-shell',
            '--dseq', str(dseq), '--gseq', gseq, '--oseq', oseq, '--provider', provider,
            '--keyring-backend', AKASH_KEYRING_BACKEND, '--from', AKASH_WALLET_NAME,
            '--node', self.akash_node, '--auth-type', 'mtls',
            '--tty', '--stdin',
            service_name, '/bin/bash'
        ]
        
        try:
            # Execute the command directly (replaces current process)
            import os
            os.execvp(cmd[0], cmd)
        except Exception as e:
            return {'success': False, 'error': f'Failed to open shell: {str(e)}'}

    def dry_run(self):
        """Validate configuration"""
        self.is_dry_run = True  # Set dry-run flag
        self.logger.info("🧪 Dry run - validating configuration...")
        
        try:
            if not self.restore_wallet():
                return {
                    'success': False,
                    'message': 'Configuration validation failed',
                    'validation_results': {'wallet': False, 'balance': False, 'certificate': False, 'rpc_node': False},
                    'error': 'Wallet restoration failed'
                }

            # Check balance
            balance_sufficient = self.get_wallet_balance() > 1000000
            
            # Check certificate (both on-chain and local file)
            self.logger.info("🔐 Checking certificate status...")
            
            # Check for local certificate file
            home_dir = os.path.expanduser("~")
            cert_dir = os.path.join(home_dir, ".akash")
            pem_file = os.path.join(cert_dir, f"{self.wallet_address}.pem")
            local_cert_exists = os.path.exists(pem_file)
            
            if local_cert_exists:
                self.logger.info(f"✅ Local certificate file found: {self.wallet_address}.pem")
            else:
                self.logger.warning(f"⚠️  Local certificate file missing: {self.wallet_address}.pem")
            
            # Check for on-chain certificate
            cert_success, cert_result = self.execute_query(['query', 'cert', 'list', '--owner', self.wallet_address])
            cert_on_chain = False
            if cert_success and isinstance(cert_result, dict) and cert_result.get('certificates'):
                cert_count = len(cert_result.get('certificates', []))
                self.logger.info(f"✅ On-chain certificate found ({cert_count} certificate(s) published)")
                cert_on_chain = True
            else:
                self.logger.warning("⚠️  No on-chain certificate found (will need to publish before deployment)")
                cert_on_chain = False
            
            # Certificate is ready if we have both local file AND on-chain cert
            # OR if we have on-chain cert (can regenerate local file)
            cert_ready = cert_on_chain  # Can always regenerate local file from on-chain cert
            
            if cert_on_chain and not local_cert_exists:
                self.logger.info("ℹ️  Local certificate file will be regenerated from on-chain certificate during deployment")

            checks = {
                'wallet': True,
                'balance': balance_sufficient,
                'certificate': cert_ready,
                'certificate_on_chain': cert_on_chain,
                'certificate_local': local_cert_exists,
                'rpc_node': bool(self.akash_node)
            }

            # Create consistent output structure with production, using placeholders for dry-run
            result = {
                'success': all(checks.values()),
                'message': 'Configuration validated successfully (dry-run)' if all(checks.values()) else 'Configuration issues found',
                'deployment_info': {
                    'dseq': None,
                    'owner': self.wallet_address,
                    'manifest_path': None
                },
                'lease_info': {
                    'provider': None,
                    'gseq': None,
                    'oseq': None
                },
                'service_url': None,
                'api_credentials': {
                    'username': None,
                    'password': None,
                    'api_url': None
                },
                'validation_results': checks,
                'cost_estimate': {
                    'estimated_cost_akt': 0.5,
                    'current_balance_akt': self.balance_uakt / 1000000,
                    'sufficient_funds': checks['balance']
                }
            }
            
            return result
            
        finally:
            # Always clean up wallet after dry-run for security
            self.cleanup_wallet()

def main():
    parser = argparse.ArgumentParser(description='Deploy ComfyUI to Akash Network')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument('--dry-run', action='store_true', help='Validate without deploying')
    parser.add_argument('--check-ready', action='store_true', help='Check if deployment is ready (services + models)')
    parser.add_argument('--close', action='store_true', help='Close active deployment')
    parser.add_argument('--status', action='store_true', help='Check lease status')
    parser.add_argument('--logs', action='store_true', help='View deployment logs')
    parser.add_argument('--shell', action='store_true', help='Get interactive shell into container')
    parser.add_argument('--rpc-info', action='store_true', help='Show RPC info')
    parser.add_argument('--cert-query', action='store_true', help='Query certificate status for wallet or --cert-owner address')
    parser.add_argument('--cert-add', action='store_true', help='Ensure certificate exists (generate/publish if missing)')
    parser.add_argument('--cert-new', action='store_true', help='Create and publish a new certificate')
    parser.add_argument('--cert-overwrite', action='store_true', help='With --cert-new: revoke existing valid cert(s) before publishing new one')
    parser.add_argument('--cert-revoke-serial', help='Revoke a specific certificate serial')
    parser.add_argument('--cert-owner', help='Wallet address owner for --cert-query (defaults to restored wallet address)')
    parser.add_argument('-y', '--yaml', help='Custom YAML manifest')
    parser.add_argument('-f', '--yaml-file', help='Path to YAML file')

    args = parser.parse_args()

    # Determine which actions don't require YAML
    query_actions = [
        args.rpc_info,
        args.check_ready,
        args.close,
        args.status,
        args.logs,
        args.shell,
        args.cert_query,
        args.cert_add,
        args.cert_new,
        bool(args.cert_revoke_serial)
    ]
    deployment_actions = [args.dry_run, (not any(query_actions))]  # dry-run or production deploy
    
    has_query_action = any(query_actions)
    has_deployment_action = any(deployment_actions)
    has_yaml = any([args.yaml, args.yaml_file])
    
    # Show help if no arguments
    if not has_query_action and not has_deployment_action:
        parser.print_help()
        sys.exit(0)
    
    # Require YAML for deployment actions (dry-run or production)
    if has_deployment_action and not has_yaml:
        error_result = {
            'success': False,
            'error': 'YAML manifest required for deployment. Use -f <file> or -y <yaml_content>',
            'message': 'Missing required YAML manifest'
        }
        print(json.dumps(error_result, indent=2), file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    deployer = AkashDeployer(debug_mode=args.debug, yaml_content=args.yaml, yaml_file=args.yaml_file)

    try:
        result = None
        
        if args.rpc_info:
            result = {'selected_node': deployer.akash_node, 'available_nodes': AKASH_RPC_NODES}
        elif args.cert_query:
            if not args.cert_owner and not deployer.restore_wallet():
                result = {'success': False, 'error': 'Wallet restoration failed'}
            else:
                result = deployer.query_certificates(owner_address=args.cert_owner)
        elif args.cert_add:
            if not deployer.restore_wallet():
                result = {'success': False, 'error': 'Wallet restoration failed'}
            else:
                result = deployer.add_certificate()
        elif args.cert_new:
            if not deployer.restore_wallet():
                result = {'success': False, 'error': 'Wallet restoration failed'}
            else:
                result = deployer.create_new_certificate(overwrite=args.cert_overwrite)
        elif args.cert_revoke_serial:
            if not deployer.restore_wallet():
                result = {'success': False, 'error': 'Wallet restoration failed'}
            else:
                result = deployer.revoke_certificate(args.cert_revoke_serial)
        elif args.dry_run:
            result = deployer.dry_run()
        elif args.check_ready:
            # Check if deployment is ready (services + models)
            result = deployer.check_ready()
        elif args.close:
            result = deployer.close_deployment()
        elif args.status:
            result = deployer.get_lease_status()
        elif args.logs:
            result = deployer.get_lease_logs()
        elif args.shell:
            # Note: get_interactive_shell() uses os.execvp and won't return
            result = deployer.get_interactive_shell()
        else:
            # Production deployment (returns immediately after manifest send)
            result = deployer.run()
        
        if result is None:
            result = {'success': False, 'error': 'Unknown command'}

        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get('success', False) else 1)

    except Exception as e:
        error_result = {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
        print(json.dumps(error_result, indent=2))
        sys.exit(1)

if __name__ == '__main__':
    main()