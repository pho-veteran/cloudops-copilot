# Single-host EC2 release lab

This OpenTofu root provisions one owned Linux EC2 instance and an encrypted gp3 root volume in `us-east-1`. The existing [HTTPS release package](../../docs/release-package.md) is built outside the EC2 lab. The [owned lab procedure](../../docs/e01-local-lab.md) establishes scoped K3s/monitoring/isolated writer inputs separately.

## Prepare a plan

Use the [pinned OpenTofu release](https://github.com/opentofu/opentofu/releases/tag/v1.13.9) and the committed AWS provider lock. Use a dedicated private state location outside the repository and keep its copies private. Run one operation at a time. Ensure any host credentials and the expected account and target are approved before querying AWS. This template does not create credentials, IAM roles, split deployment environments or run a cloud-init provisioner.

Provide each required input in a private variables file. Verify the exact regional x86-64 EBS-backed Linux AMI and its approved publisher account, the existing SSH key, and an isolated availability zone. The plan checks the advertised x86-64 architecture, 8 vCPU and 10 GiB RAM minimum. The installed lab verifies actual available resources. Provision enough disk to leave 20 GiB free after installation and image loading. The original 4 vCPU / 8 GiB / 40 GiB sizing was a planning assumption. Measure actual idle and representative bounded load after authorization, including disk, cgroups, thresholds, pressure and candidate/surge headroom.

```sh
cd deploy/ec2
tofu fmt -check
tofu init -backend=false -lockfile=readonly
tofu validate
```

After the account owner approves the account, region, target, costs, timebox and cleanup policy, initialize the authorized private state and review the exact saved plan. Supply absolute paths for the state, input and plan artifacts outside this checkout. Apply only that reviewed, budget-authorized plan with the confirmed account context.

```sh
(
  set -eu
  umask 077
  : "${private_state_file:?Set the dedicated local state path}"
  : "${approved_variables:?Set the reviewed variable file path}"
  : "${reviewed_plan:?Set the saved plan destination}"
  for path in "$private_state_file" "$approved_variables" "$reviewed_plan"; do
    case "$path" in
      /*) ;;
      *) printf '%s\n' 'Use absolute paths for state, variables and plan' >&2; exit 1 ;;
    esac
  done
  export TF_DATA_DIR="$private_state_file.tofu" TF_WORKSPACE=default
  unset TF_CLI_ARGS TF_CLI_ARGS_init TF_CLI_ARGS_plan
  mkdir -p "$TF_DATA_DIR"
  tofu init -reconfigure -backend-config="path=$private_state_file" -lockfile=readonly
  tofu plan -var-file="$approved_variables" -out="$reviewed_plan"
)
```

Take plans only while the instance is running. A stopped instance reports `associate_public_ip_address` drift, and that drift forces replacement.

The plan requires a root-only EBS AMI and should show the single instance, one encrypted volume and the dedicated network. Only SSH from the approved IPv4 `/32` is accepted. Network egress permits IPv4 TCP 443. The app's separately approved source and provider policies still govern its allowed destinations. Application, database, Kubernetes and executor ports stay unexposed. Public IPv4 is assigned to this instance for management and internet reachability. Obtain the approved SSH host key independently and put it in the operator's known_hosts before connecting.

## Qualified configuration

The [S2 acceptance](../../docs/evidence/e10-s2-ec2/README.md) qualified this configuration:

| Item | Value |
| --- | --- |
| AMI | Official Debian 13 (`debian-13-amd64-*`, publisher `136693071363`), which has only an EBS root mapping. SSH user `admin`. Ubuntu AMIs add ephemeral mappings and fail the precondition. Amazon Linux 2023 ships Docker 25, below the lab's 28+ requirement. |
| Instance | `c6a.2xlarge` (USD 0.306/h). Use `c5a.2xlarge` (USD 0.308/h) when the AZ has no `c6a` capacity. Avoid burstable types, whose unlimited credits add cost under the K3s lab. |
| Root volume | gp3 50 GiB. The owned lab builds its images on the host. After setup about 45 GiB was free, and at least 38 GiB remained during the lab. |

Prepare the host before installing:

1. Arm an automatic stop at the start of every session, for example `sudo shutdown -h +300`. The default shutdown behavior of EC2 is stop.
2. Switch every apt source to `https://` before the first update, because egress is TCP 443 only.
3. Install Docker CE, Buildx and the Compose plugin from `download.docker.com`. Verify the signing key fingerprint `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`.
4. Install Python 3.12 or later, Git and the pinned `uv` release with its published SHA-256.
5. Obtain the SSH host keys from the full `aws ec2 get-console-output` output, without `--latest`, and pin them before the first connection.

Host constraints:

- **Swap limits:** a systemd `daemon-reload`, for example from apt-listchanges, resets the Docker scopes' `memory.swap.max` to `max` under the systemd cgroup driver. The lab then refuses load. Pause the apt timers for the session. If the reset happens anyway, re-apply each container's own `--memory`/`--memory-swap` values with `docker update`.
- **Checkout permissions:** clone the trusted checkout under `umask 022`. The sample container user must be able to read the source files.
- **Docker Hub rate limit:** registry verification for the owned lab can reach Docker Hub's anonymous limit for the instance's public address. A stop/start obtains a new address. Do not add credentials or mirrors.
- **Instance type changes:** the owned lab sizes kubelet reserves on first start. After changing the instance type, stop the lab, run its ownership-scoped cleanup and start it fresh.

## Install and reach the existing origin

Verify prerequisites on the host and transfer the pinned package over the authorized connection. Follow the [release package install](../../docs/release-package.md) to verify the core artifact. Stop that core project with volume retention before starting the separate owned lab project. Both use loopback port 8443 and need separate project volumes. Qualify scoped services and reader/writer isolation using the owned lab procedure. Run the tunnel from the operator workstation while the app is up. Replace the SSH user and address with the approved AMI user and current instance output.

```sh
ssh -N -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no \
  -L 127.0.0.1:8443:127.0.0.1:8443 "$ssh_user@$host"
```

Use `https://localhost:8443` via the tunnel. Use the CA for the active project and the workstation's approved trust process. Keep the certificate hostname, configured Origin and address identical. Release qualification must record actual install, sessions, scoped source readiness and reader/writer isolation.

## Estimate, stop and retain

Before spending, fill in a dated worksheet from [EC2](https://aws.amazon.com/ec2/pricing/on-demand/), [EBS](https://aws.amazon.com/ebs/pricing/), [VPC IPv4](https://aws.amazon.com/vpc/pricing/) and [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html). The earlier $25–30 target, $40 reserve and 40–60 estimated compute hours are planning inputs. An acceptable envelope includes stop deadlines and residual costs even if the campaign ends early.

| Item | Estimate to record |
| --- | --- |
| Linux instance | Dated regional rate × approved running hours, including any applicable burst credits |
| gp3 | GiB × billable retention time, plus paid IOPS/throughput if selected |
| Public IPv4 | Dated hourly rate × assigned time, including any retained address |
| Snapshots | Estimated billable snapshot GiB and retention |
| Data transfer | Expected billable GB by destination and applicable rates |
| Total and stop | Expected and upper-bound cost, approved ceiling, owner, stop deadline and actual resource inventory |

Billing alerts can arrive after charges. Use an explicit timebox and manual stop confirmation. Do not proceed if the estimate exceeds authority. Update it from measured idle/representative load and the actual retained resource list, and obtain renewed approval for any increase.

Before stopping, stop authorized load and job admission safely, preserve evidence and follow the lab's scoped stop procedure. Then stop the exact authorized instance. Confirm stopped state and record its identity, root volume, snapshots and remaining IP or transfer charges. On restart, recheck the assigned IP, HTTPS, source identities, worker status and dispatch before work. The root volume survives termination by design and remains billable until deliberately cleaned up. Treat teardown and independent EBS/snapshot deletion as separate owner decisions after matching the exact project inventory and backup policy. Keep the private state and restore record with the handoff.

## Destroy and redeploy

When the owner approves disposal, as for a demo lab:

1. Copy the sanitized evidence off the host and run the owned lab's `cleanup`.
2. Save and review the destroy plan, then apply only that plan.

```sh
tofu plan -destroy -var-file="$approved_variables" -out="$reviewed_destroy_plan"
tofu apply "$reviewed_destroy_plan"
```

3. Find the retained root volume. It is tagged with the lab's `Project` and named `<lab_name>-retained-root`. Confirm it is `available` and has no attachment, then delete it with `aws ec2 delete-volume`.
4. Confirm the project inventory is empty: tagged resources, instances, volumes, snapshots, Elastic IPs, network interfaces, spot requests and `tofu state list`.

The key pair, budget, private variables and empty state cost nothing. Keep them for a redeploy.

To redeploy:

1. Update the operator `/32` in the variables file.
2. Re-check the newest Debian AMI ID, its publisher and its mapping.
3. Save and review a new plan, then apply it.
4. Repeat the host preparation and the release install.

In the S2 run, apply, host preparation and the core install took about 12 minutes of instance time. Plan for 30 minutes before the lab is usable.
