# MockingBot Cloud Deployment Checklist

Status: Planning document — incomplete by design  
Established: July 17, 2026

## Purpose

This is the living checklist for eventually moving MockingBot to a cloud-hosted
environment. Cloud hosting is new territory for the owner, so decisions and
instructions will be added incrementally as questions are answered. Nothing in
this document authorizes a live deployment or replaces the live preflight.

## Decision Log

### Remote manual trade rejection

Problem discussed:

- A trade may be obviously undesirable even though the copied wallet remains in
  it—for example, a long position during a strong downward market trend.
- Closing it directly in Hyperliquid bypasses the bot's normal close workflow.
- The bot cannot safely infer that every externally disappeared position was a
  deliberate rejection; it could instead be liquidation, other automation, or
  a synchronization fault.
- Without an explicit rejection record, the wallet-scoring engine receives no
  discretionary penalty.
- A local PowerShell-only command is insufficient when the bot runs remotely
  and the owner does not have direct server access.

Proposed solution:

- Provide a secure remote **Reject & Close** control.
- Keep the monitoring dashboard read-only. Use a separate, narrowly scoped
  control service or control page rather than adding general write access to
  the dashboard.
- The control service writes a durable, uniquely identified request to a bot
  command queue.
- The running bot validates and consumes the request during its normal cycle,
  then closes the position through the existing durable intent,
  confirmation, and reconciliation path.
- Record the confirmed exchange fill, realized result, operator reason, command
  status, timestamps, account, coin, and affected source wallets.
- Apply a persistent `-1.0` score adjustment to each distinct source wallet for
  that rejected trade. Stacked allocations from the same wallet receive one
  penalty, not one penalty per slice.
- The normal realized PnL effect remains part of scoring; the extra point records
  the discretionary judgment that the trade required intervention.
- Idempotency must prevent the close or score penalty from being applied twice.
- Status must be visible as pending, executing, confirmed, failed, or ambiguous.
- A failure or ambiguity must quarantine only the affected coin while the rest
  of the book continues.

Initial penalty decision:

- Start at one point per rejected trade.
- Keep the penalty separately visible in the wallet score explanation and audit
  history.
- Reassess its size only after enough manual-rejection outcomes exist to measure
  whether one point is too weak or too strong.

Security requirements already identified:

- Do not expose arbitrary shell commands or generic administrative execution.
- Never send the Hyperliquid API private key to a browser.
- Require authenticated, encrypted access and preferably a private network or
  VPN such as Tailscale.
- Restrict access to specifically authorized users/devices and the minimum
  required endpoint.
- Use short request lifetimes, unique command IDs, duplicate/replay protection,
  explicit account-and-coin confirmation, and complete audit logs.

## Evolving Deployment Checklist

### 1. Define requirements

- [ ] Decide the cloud provider only after comparing simplicity, cost, Windows
      versus Linux support, backups, and recovery.
- [ ] Decide whether deployment will use a native service or Docker.
- [ ] Estimate storage, CPU, memory, bandwidth, and expected monthly cost.
- [ ] Choose the deployment region and timezone/logging convention.
- [ ] Define acceptable downtime and recovery time.
- [ ] Decide how Paper, Live, Elite, comparison, dashboard, and remote control
      processes will be separated.

### 2. Prepare the application

- [ ] Document every required Python and system dependency.
- [ ] Create a reproducible installation/startup procedure.
- [ ] Add service health checks and process supervision.
- [ ] Define graceful shutdown and restart behavior.
- [ ] Verify persistent data paths do not live inside an ephemeral container.
- [ ] Confirm single-instance locks work in the selected environment.
- [ ] Design and test the durable remote command queue.
- [ ] Implement and test **Reject & Close** end to end.

### 3. Credentials and secrets

- [ ] Inventory required credentials without copying their values into this
      document or Git.
- [ ] Select a cloud secret-storage mechanism.
- [ ] Define initial secret installation and later rotation procedures.
- [ ] Ensure logs, crash reports, backups, and dashboards cannot disclose keys.
- [ ] Confirm the API wallet is linked to the intended main account during every
      live preflight.

### 4. Network and remote access

- [ ] Decide between private VPN access, a zero-trust access gateway, or another
      authenticated HTTPS design.
- [ ] Keep database and bot control ports off the public internet.
- [ ] Restrict inbound and outbound network access to required destinations.
- [ ] Configure TLS, authentication, authorization, and rate limits.
- [ ] Test access revocation from a lost or replaced device.
- [ ] Define emergency access if the preferred remote-access service is down.

### 5. Persistent data and backups

- [ ] Provision persistent storage for each bot instance.
- [ ] Keep Paper and Live databases strictly isolated.
- [ ] Schedule verified SQLite online backups.
- [ ] Copy backups to a separate failure domain or storage service.
- [ ] Encrypt backups and define retention.
- [ ] Perform and document a full restore test before going live.

### 6. Monitoring and alerts

- [ ] Monitor process health, last successful cycle, API availability, equity
      freshness, reconciliation, quarantine, unresolved intents, and backups.
- [ ] Configure alerts that reach the owner without requiring dashboard checks.
- [ ] Distinguish warning, coin-level quarantine, and account-level breaker
      severity.
- [ ] Prevent repeated alerts from flooding the notification channel.
- [ ] Define log rotation and retention.

### 7. Security review

- [ ] Run the service as a non-administrator/non-root user.
- [ ] Patch the operating system and dependencies.
- [ ] Review firewall and access policies using least privilege.
- [ ] Threat-model the remote rejection endpoint and command queue.
- [ ] Test authentication failure, replay, duplicate commands, stale commands,
      wrong-account commands, and interrupted execution.
- [ ] Establish an incident-response and credential-rotation checklist.

### 8. Staged deployment

- [ ] Deploy Paper first with no live credentials.
- [ ] Compare local and cloud Paper results for configuration and signal parity.
- [ ] Test restarts, host reboots, network loss, storage pressure, backup restore,
      and alert delivery.
- [ ] Run the full automated test suite and read-only live preflight in cloud.
- [ ] Verify the live account is flat before first initialization.
- [ ] Begin with the approved four-slot, 3x-leverage live configuration.
- [ ] Observe the first entry, add, partial exit, full exit, reconciliation, and
      restart recovery before considering unattended operation.
- [ ] Do not expand slots or leverage until cloud behavior is verified.

## Questions to Resolve Later

- Which cloud provider and operating system best match the owner's comfort and
  the bot's operational needs?
- Docker or conventional system service?
- Which private-access method should be used?
- Which notification service should deliver urgent alerts?
- Should **Reject & Close** be a small web page, mobile-friendly control, or an
  integration with a trusted messaging service?
- What confirmation step is appropriate before a remote close?
- Should manual score penalties ever expire, decay, or require later review?
- How should a direct Hyperliquid closure be retrospectively classified and
  reconciled if the remote control was unavailable?

## Change Log

- July 17, 2026: Created the planning file and recorded the remote manual-trade
  rejection discussion, one-point penalty proposal, security boundaries, and
  initial staged cloud checklist.
