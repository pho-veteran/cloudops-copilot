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

The plan requires a root-only EBS AMI and should show the single instance, one encrypted volume and the dedicated network. Only SSH from the approved IPv4 `/32` is accepted. Network egress permits IPv4 TCP 443. The app's separately approved source and provider policies still govern its allowed destinations. Application, database, Kubernetes and executor ports stay unexposed. Public IPv4 is assigned to this instance for management and internet reachability. Obtain the approved SSH host key independently and put it in the operator's known_hosts before connecting.

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
