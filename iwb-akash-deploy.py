#!/usr/bin/env python3
"""
iwb-akash-deploy.py - Compact Akash Deployment Script
Deploy ComfyUI instances to Akash Network for n8n workflows
"""

__version__ = "1.0.1"

import os, sys, json, subprocess, time, secrets, string, argparse, requests, concurrent.futures
import stat, shutil, traceback, logging, yaml, tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List, Any
from pathlib import Path

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

class AkashDeployer:
    """Main deployer class - compact version"""
    
    def __init__(self, debug_mode=False, dseq=None, yaml_content=None, yaml_file=None):
        self.debug_mode = debug_mode
        self.is_dry_run = False  # Flag to track if we're in dry-run mode
        self.dseq = dseq
        self.yaml_content = yaml_content
        self.yaml_file = yaml_file
        self.wallet_address = None
        self.wallet_mnemonic = None
        self.balance_uakt = 0
        self.akash_node = None  # Will be set after logger initialization
        self.logger = self._setup_logging()
        self.state_file = self._get_state_file()
        # Now select RPC node with proper logging
        self.akash_node = self._select_fastest_rpc_node()

    def _setup_logging(self):
        log_file = self._get_log_file_path()
        level = logging.DEBUG if self.debug_mode else logging.INFO
        handlers: List[logging.Handler] = [logging.FileHandler(log_file, mode='a')]
        if self.debug_mode:
            handlers.append(logging.StreamHandler(sys.stderr))
        logging.basicConfig(level=level, format='%(asctime)s - %(levelname)s - %(message)s', handlers=handlers)
        logger = logging.getLogger(__name__)
        logger.info("=" * 50)
        return logger

    def _get_log_file_path(self):
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
        
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        suffix = f"_{self.dseq}" if self.dseq else ""
        return str(base_dir / f"iwb-akash-deploy_{timestamp}{suffix}.log")

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
            try:
                # Try JSON first
                return True, json.loads(stdout)
            except json.JSONDecodeError:
                try:
                    # Try YAML if JSON fails
                    return True, yaml.safe_load(stdout)
                except yaml.YAMLError:
                    # Return raw string if both fail
                    return True, stdout
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
            for balance in balances:
                if balance.get('denom') == 'uakt':
                    amount = int(balance.get('amount', 0))
                    self.balance_uakt = amount
                    self.logger.info(f"💰 Balance: {amount / 1000000:.2f} AKT")
                    return amount
            # If no uakt balance found, wallet might be empty
            self.logger.info(f"💰 Balance: 0.00 AKT (no uakt balance found)")
        else:
            self.logger.error(f"Failed to get balance: success={success}, result={result}")
        return 0

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
        
        # Check if certificate exists on-chain (result must be dict with certificates)
        if success and isinstance(result, dict) and result.get('certificates'):
            cert_count = len(result.get('certificates', []))
            self.logger.info(f"✅ Certificate already published ({cert_count} certificate(s) found for this wallet)")
            
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
                success, stdout, stderr = self.execute_tx(['tx', 'cert', 'generate', 'client'])
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
        success, stdout, stderr = self.execute_tx(['tx', 'cert', 'generate', 'client'])
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

    def create_deployment_manifest(self, api_credentials):
        """Return path to manifest file - use provided file from n8n or yaml content directly"""
        # If a YAML file was provided (e.g., from n8n at /tmp/deploy.yaml), use it directly
        if self.yaml_file:
            self.logger.info(f"📄 Using provided YAML file: {self.yaml_file}")
            return self.yaml_file
        
        # If YAML content was provided as string, return it directly (provider-services can handle YAML content)
        if self.yaml_content:
            self.logger.info(f"📄 Using provided YAML content")
            return self.yaml_content
        
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

    def create_deployment(self):
        """Create deployment"""
        self.logger.info("📦 Creating deployment...")
        manifest_path = self.create_deployment_manifest(self.generate_api_credentials())
        success, stdout, stderr = self.execute_tx(['tx', 'deployment', 'create', manifest_path])
        
        if not success:
            self.logger.error(f"❌ Deployment creation failed: {stderr}")
            return {'success': False, 'error': f'Deployment creation failed: {stderr}'}

        self.logger.debug(f"Deployment creation output: {stdout}")
        
        # Parse DSEQ from output - try multiple methods
        dseq = None
        
        # Method 1: Try to parse as JSON first
        try:
            output_data = json.loads(stdout)
            if isinstance(output_data, dict):
                # Look for DSEQ in the transaction events
                txhash = output_data.get('txhash')
                if txhash:
                    self.logger.info(f"Got transaction hash: {txhash}")
                
                # Try to extract DSEQ from transaction logs/events
                logs = output_data.get('logs', [])
                for log in logs:
                    events = log.get('events', [])
                    for event in events:
                        if event.get('type') == 'akash.v1':
                            attributes = event.get('attributes', [])
                            for attr in attributes:
                                if attr.get('key') == 'dseq':
                                    dseq = attr.get('value')
                                    if dseq:
                                        break
                        if dseq:
                            break
                    if dseq:
                        break
                
                # If not found in logs, try the raw_log field
                if not dseq:
                    raw_log = output_data.get('raw_log', '')
                    import re
                    # Look for "dseq":"number" pattern in raw_log
                    match = re.search(r'"dseq":"(\d+)"', raw_log)
                    if match:
                        dseq = match.group(1)
                        
        except (json.JSONDecodeError, Exception) as e:
            self.logger.debug(f"JSON parsing failed: {e}")
        
        # Method 2: Parse text output
        if not dseq:
            for line in stdout.split('\n'):
                line = line.strip()
                # Look for patterns like "deployment created: 123456" or "dseq: 123456"
                if any(keyword in line.lower() for keyword in ['deployment', 'created', 'dseq']):
                    parts = line.split()
                    for part in parts:
                        # Check if it's a number (DSEQ is usually 6-8 digits)
                        if part.isdigit() and len(part) >= 6:
                            dseq = part
                            break
                if dseq:
                    break
        
        # Method 3: Try to extract from any line with digits
        if not dseq:
            import re
            for line in stdout.split('\n'):
                # Look for sequences of 6+ digits
                matches = re.findall(r'\b\d{6,}\b', line)
                if matches:
                    dseq = matches[0]  # Take the first long number we find
                    break

        if not dseq:
            self.logger.error(f"Failed to parse DSEQ from output: {stdout}")
            return {'success': False, 'error': f'Failed to parse deployment output. Raw output: {stdout}'}

        self.logger.info(f"✅ Deployment created with DSEQ: {dseq}")
        deployment_info = {'dseq': dseq, 'owner': self.wallet_address, 'manifest_path': manifest_path}
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
            
            # Use the correct bid query - the command syntax works, timeouts are RPC-node specific
            success, result = self.execute_query([
                'query', 'market', 'bid', 'list', 
                '--dseq', dseq, '--owner', self.wallet_address, '--state', 'open'
            ])
            
            self.logger.debug(f"Bid check #{bid_check_count}: success={success} (RPC: {self.akash_node})")
            if self.debug_mode and bid_check_count <= 2:
                self.logger.debug(f"Raw bid query result: {result}")
            
            if success and isinstance(result, dict):
                bids = result.get('bids', [])
                if bids:
                    self.logger.info(f"✅ Received {len(bids)} open bids for DSEQ {dseq}")
                    return bids
                else:
                    self.logger.debug(f"No open bids yet for DSEQ {dseq}")
            elif success:
                self.logger.debug(f"Bid query returned non-dict result: {type(result)} - {result}")
            else:
                self.logger.debug(f"Bid query failed on {self.akash_node}: {result}")
                
                # Try a different approach - query without state filter as fallback
                if bid_check_count % 3 == 0:  # Every 3rd attempt, try different approach
                    self.logger.debug(f"Trying bid query without state filter as fallback...")
                    fallback_success, fallback_result = self.execute_query([
                        'query', 'market', 'bid', 'list', '--dseq', dseq, '--owner', self.wallet_address
                    ])
                    
                    if fallback_success and isinstance(fallback_result, dict):
                        all_bids = fallback_result.get('bids', [])
                        open_bids = [bid for bid in all_bids 
                                   if bid.get('bid', {}).get('state') == 'open']
                        if open_bids:
                            self.logger.info(f"✅ Found {len(open_bids)} open bids via fallback query")
                            return open_bids
                        elif all_bids:
                            closed_bids = [bid for bid in all_bids if bid.get('bid', {}).get('state') == 'closed']
                            self.logger.debug(f"Found {len(all_bids)} total bids ({len(closed_bids)} closed) - no open bids")
                    else:
                        self.logger.debug(f"Fallback bid query also failed: {fallback_result}")
            
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
        
        scored_bids = []
        for bid in bids:
            provider = bid['bid']['bid_id']['provider']
            
            # Get provider attributes
            provider_attrs = self._get_provider_attributes(provider)
            if not provider_attrs:
                self.logger.warning(f"⚠️  Skipping bid from {provider[:20]}... - no attributes available")
                continue
            
            # Score the provider
            score = self._score_provider(provider, provider_attrs)
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
                'attributes': provider_attrs
            })
            
            self.logger.info(f"  📊 {provider} - Score: {score:.1f}, Price: {price} uakt, Combined: {combined_score:.1f}")
        
        if not scored_bids:
            self.logger.error("❌ No valid bids after scoring")
            return None
        
        # Sort by combined score (highest first)
        scored_bids.sort(key=lambda x: x['combined_score'], reverse=True)
        
        best = scored_bids[0]
        self.logger.info(f"✅ Selected best bid: {best['provider']} (Score: {best['combined_score']:.1f})")
        self.logger.info(f"📍 Provider URL: {best['provider_url']}")
        
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

    def _score_provider(self, provider_address, attributes):
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
        gpu_preferences = self._get_gpu_preferences_from_manifest()
        gpu_model = attr_dict.get('capabilities/gpu/model', '').lower()
        
        if gpu_preferences and gpu_model:
            for i, preferred_gpu in enumerate(gpu_preferences):
                if preferred_gpu.lower() in gpu_model:
                    # Higher score for higher priority GPUs
                    score += 100 - (i * 10)
                    break
        elif attr_dict.get('capabilities/gpu/vendor') == 'nvidia':
            score += 25  # Basic NVIDIA support
        
        # Organization quality
        organization = attr_dict.get('organization', '').lower()
        if 'overclock' in organization:
            score += 20
        elif 'datacenter' in attr_dict.get('location-type', '').lower():
            score += 15
        
        return score

    def _get_gpu_preferences_from_manifest(self):
        """Extract GPU preferences from manifest"""
        try:
            manifest = None
            if self.yaml_file:
                with open(self.yaml_file, 'r') as f:
                    manifest = yaml.safe_load(f.read())
            elif self.yaml_content:
                manifest = yaml.safe_load(self.yaml_content)
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
                
                # If we found GPU preferences, use them
                if gpu_preferences:
                    self.logger.info(f"📋 GPU preferences from manifest: {gpu_preferences}")
                    return gpu_preferences
            
            # Default GPU preference order if not specified in manifest
            default_prefs = ['rtx4090', 'a100', 'h100', 'rtx3090', 'rtx3080', 'v100', 'a6000']
            self.logger.info(f"📋 Using default GPU preferences: {default_prefs}")
            return default_prefs
            
        except Exception as e:
            self.logger.warning(f"⚠️  Could not extract GPU preferences: {e}")
            default_prefs = ['rtx4090', 'a100', 'h100']
            return default_prefs

    def create_lease(self, dseq, bid):
        """Create lease and save provider info"""
        provider = bid['bid']['bid_id']['provider']
        gseq = str(bid['bid']['bid_id']['gseq'])
        oseq = str(bid['bid']['bid_id']['oseq'])
        
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
            return {'success': False, 'error': 'No provider information found in deployment state'}
        
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
                    status_data = json.loads(stdout) if stdout else {}
                except json.JSONDecodeError:
                    status_data = yaml.safe_load(stdout) if stdout else {}
                
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

    def run(self):
        """Main deployment workflow"""
        try:
            # IMPORTANT: Restore wallet FIRST so we have wallet_address for checking existing deployments
            if not self.restore_wallet():
                return {
                    'success': False,
                    'message': 'Deployment failed',
                    'error': 'Wallet restoration failed',
                    'deployment_info': None,
                    'lease_info': None,
                    'service_url': None,
                    'api_credentials': None
                }

            # Now check for existing active deployment (wallet_address is set)
            has_active, active_deployment_info = self.has_active_deployment()
            if has_active and active_deployment_info:
                # We have a verified active deployment
                dseq = active_deployment_info.get('dseq')
                self.logger.info(f"✅ Using existing active deployment: DSEQ {dseq}")
                
                # Get service URL if not already in state
                service_url = active_deployment_info.get('service_url', '')
                if not service_url:
                    # Pass deployment_info so check_service_status has access to provider details
                    service_url = self.get_service_url_from_lease(dseq, active_deployment_info)
                    if service_url:
                        # Update state with service URL
                        active_deployment_info['service_url'] = service_url
                        self.save_state(active_deployment_info)
                
                # Get or generate API credentials if not already in state
                api_credentials = active_deployment_info.get('api_credentials', {})
                if not api_credentials:
                    api_credentials = self.generate_api_credentials(service_url)
                    # Update state with API credentials
                    active_deployment_info['api_credentials'] = api_credentials
                    self.save_state(active_deployment_info)
                elif api_credentials.get('api_url') == 'http://service-url-placeholder' and service_url:
                    # Update placeholder with actual URL
                    api_credentials['api_url'] = service_url
                    active_deployment_info['api_credentials'] = api_credentials
                    self.save_state(active_deployment_info)
                
                return {
                    'success': True,
                    'deployment_info': active_deployment_info,
                    'api_credentials': api_credentials,
                    'service_url': service_url,
                    'message': f"Using existing active deployment: DSEQ {dseq}"
                }

            # No existing deployment found, continue with checks for new deployment
            if self.get_wallet_balance() < 1000000:
                return {
                    'success': False,
                    'message': 'Deployment failed',
                    'error': 'Insufficient balance',
                    'deployment_info': None,
                    'lease_info': None,
                    'service_url': None,
                    'api_credentials': None
                }

            if not self.setup_certificate():
                return {
                    'success': False,
                    'message': 'Deployment failed',
                    'error': 'Certificate setup failed',
                    'deployment_info': None,
                    'lease_info': None,
                    'service_url': None,
                    'api_credentials': None
                }

            # Create deployment
            result = self.create_deployment()
            if not result['success']:
                return result

            dseq = result['deployment_info']['dseq']
            
            # Wait for bids
            bids = self.wait_for_bids(dseq)
            if not bids:
                return {
                    'success': False,
                    'message': 'Deployment failed',
                    'error': 'No bids received',
                    'deployment_info': result.get('deployment_info'),
                    'lease_info': None,
                    'service_url': None,
                    'api_credentials': None
                }

            # Create lease
            best_bid = self.select_best_bid(bids)
            lease_result = self.create_lease(dseq, best_bid)
            if not lease_result['success']:
                return lease_result

            provider = lease_result['lease_info']['provider']
            
            # Send manifest and wait
            manifest_path = result['deployment_info'].get('manifest_path')
            if not manifest_path:
                return {
                    'success': False,
                    'message': 'Deployment failed',
                    'error': 'Manifest path not found in deployment info',
                    'deployment_info': result.get('deployment_info'),
                    'lease_info': lease_result.get('lease_info'),
                    'service_url': None,
                    'api_credentials': None
                }
            
            manifest_result = self.send_manifest(manifest_path, dseq)
            if not manifest_result['success']:
                return {
                    'success': False,
                    'message': 'Deployment failed',
                    'error': f"Manifest send failed: {manifest_result.get('error', 'Unknown error')}",
                    'deployment_info': result.get('deployment_info'),
                    'lease_info': lease_result.get('lease_info'),
                    'service_url': None,
                    'api_credentials': None
                }

            service_url = self.wait_for_ready(dseq, provider)
            if not service_url:
                return {
                    'success': False,
                    'message': 'Deployment failed',
                    'error': 'Service failed to become ready',
                    'deployment_info': result.get('deployment_info'),
                    'lease_info': lease_result.get('lease_info'),
                    'service_url': None,
                    'api_credentials': None
                }

            # Generate final API credentials with service URL
            api_credentials = self.generate_api_credentials(service_url)
            
            # Update deployment state with final info
            final_deployment_state = self.load_state()
            if final_deployment_state:
                final_deployment_state.update({
                    'service_url': service_url,
                    'api_credentials': api_credentials,
                    'status': 'ready'
                })
                self.save_state(final_deployment_state)

            # Send success notification email
            try:
                subject = f"Akash Deployment {dseq} Created Successfully"
                body = f"""ComfyUI Deployment Created

DSEQ: {dseq}
Provider: {provider}
Service URL: {service_url}
Time: {datetime.now(timezone.utc).isoformat()}Z

API Credentials:
- Username: {api_credentials['username']}
- Password: {api_credentials['password']}

The deployment is ready to use.
"""
                self.send_email(subject, body)
            except Exception as e:
                self.logger.warning(f"⚠️ Could not send deployment notification: {e}")

            return {
                'success': True,
                'message': 'ComfyUI deployment successful',
                'deployment_info': result['deployment_info'],
                'lease_info': lease_result['lease_info'],
                'service_url': service_url,
                'api_credentials': api_credentials
            }

        except Exception as e:
            self.logger.error(f"❌ Deployment failed: {e}")
            return {
                'success': False,
                'message': 'Deployment failed with exception',
                'error': str(e),
                'deployment_info': None,
                'lease_info': None,
                'service_url': None,
                'api_credentials': None
            }

    def close_deployment(self, dseq=None):
        """Close deployment"""
        try:
            if not dseq:
                # Restore wallet first to have wallet_address for queries
                if not self.restore_wallet():
                    return {'success': False, 'error': 'Wallet restoration failed'}
                
                # Check for active deployment (this will query blockchain if needed)
                has_active, deployment_info = self.has_active_deployment()
                if not has_active or not deployment_info:
                    return {'success': False, 'error': 'No active deployment found'}
                
                dseq = deployment_info.get('dseq')

            self.logger.info(f"🛑 Closing deployment {dseq}...")
            
            # Close the deployment first
            success, stdout, _ = self.execute_tx(['tx', 'deployment', 'close', '--dseq', dseq])
            
            if success:
                self.clear_state()
                
                # Extract transaction fee from close transaction
                tx_fee_akt = 0.0
                lease_cost_akt = 0.0
                
                try:
                    tx_data = json.loads(stdout)
                    fee_info = tx_data.get('tx', {}).get('auth_info', {}).get('fee', {})
                    for amount in fee_info.get('amount', []):
                        if amount.get('denom') == 'uakt':
                            tx_fee_akt = float(amount['amount']) / 1000000
                            break
                except (json.JSONDecodeError, KeyError):
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
                    subject = f"Akash Deployment {dseq} Closed - Cost Report"
                    body = f"""Deployment Closure Report

DSEQ: {dseq}
Closed: {datetime.now(timezone.utc).isoformat()}Z

Cost Analysis:
- Lease Cost: {lease_cost_akt:.6f} AKT
- Transaction Fee: {tx_fee_akt:.6f} AKT
- Total Cost: {total_cost_akt:.6f} AKT

{usd_info}

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
        # Restore wallet first to have wallet_address for queries
        if not self.restore_wallet():
            return {'success': False, 'error': 'Wallet restoration failed'}
        
        # Check for active deployment (this will query blockchain if needed)
        has_active, deployment_info = self.has_active_deployment()
        if not has_active or not deployment_info:
            return {'success': False, 'error': 'No active deployment found'}
        
        dseq = deployment_info.get('dseq')
        provider = deployment_info.get('provider', '')
        
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
        # Restore wallet first to have wallet_address for queries
        if not self.restore_wallet():
            return {'success': False, 'error': 'Wallet restoration failed'}
        
        # Check for active deployment (this will query blockchain if needed)
        has_active, deployment_info = self.has_active_deployment()
        if not has_active or not deployment_info:
            return {'success': False, 'error': 'No active deployment found'}
        
        dseq = deployment_info.get('dseq')
        provider = deployment_info.get('provider', '')
        gseq = deployment_info.get('gseq', '1')
        oseq = deployment_info.get('oseq', '1')
        
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
        # Restore wallet first to have wallet_address for queries
        if not self.restore_wallet():
            return {'success': False, 'error': 'Wallet restoration failed'}
        
        # Check for active deployment (this will query blockchain if needed)
        has_active, deployment_info = self.has_active_deployment()
        if not has_active or not deployment_info:
            return {'success': False, 'error': 'No active deployment found'}

        dseq = deployment_info.get('dseq')
        provider = deployment_info.get('provider')
        gseq = deployment_info.get('gseq', '1')
        oseq = deployment_info.get('oseq', '1')

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
    parser.add_argument('--close', action='store_true', help='Close active deployment')
    parser.add_argument('--status', action='store_true', help='Check lease status')
    parser.add_argument('--logs', action='store_true', help='View deployment logs')
    parser.add_argument('--shell', action='store_true', help='Get interactive shell into container')
    parser.add_argument('--rpc-info', action='store_true', help='Show RPC info')
    parser.add_argument('-y', '--yaml', help='Custom YAML manifest')
    parser.add_argument('-f', '--yaml-file', help='Path to YAML file')

    args = parser.parse_args()

    # Determine which actions don't require YAML
    query_actions = [args.rpc_info, args.close, args.status, args.logs, args.shell]
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
        elif args.dry_run:
            result = deployer.dry_run()
        elif args.close or args.status or args.logs or args.shell:
            # These commands need wallet restored to access deployment info
            if not deployer.restore_wallet():
                result = {
                    'success': False,
                    'error': 'Wallet restoration failed',
                    'message': 'Cannot access deployment information without wallet'
                }
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
            # Production deployment
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