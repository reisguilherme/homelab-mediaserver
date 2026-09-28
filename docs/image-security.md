# Public image vulnerability scans

Run only images built from public source or native upstream images. Do not pass
container IDs, runtime directories, operator `.env` files, or a full Docker inventory.

```bash
bash scripts/check-images.sh --output-dir .runtime/image-security \
  homeserver-control:dev homeserver-telemetry:dev
```

The script resolves each explicit reference to its local image ID, exports that image
to a private temporary tar, scans the tar, then removes it. It does not pull requested
images or inspect running containers. JSON inventory records contain only the requested
reference, immutable image ID and scanner version. Vulnerability reports retain every
HIGH and CRITICAL finding; no blanket exception list is used.

Exit statuses:

| Status | Meaning |
| --- | --- |
| 0 | Scan completed; no fixable CRITICAL vulnerability found. HIGH or unfixed CRITICAL findings may remain in reports. |
| 1 | At least one fixable CRITICAL vulnerability found. |
| 2 | Invalid or missing explicit image references. |
| 3 | Unknown: missing tool, export/download/database/scanner error, or invalid report. |

Trivy 0.73.0 is downloaded from its official GitHub release. The Linux x86_64 and
ARM64 archive SHA256 values are pinned in the script from the official release
checksum manifest. Download attempts and scanner execution are bounded; a fresh
vulnerability database is required. The scanner receives an empty environment and
private empty home/config/cache, and no Docker socket is mounted into a scanner image.
These checksum pins authenticate the selected archives against the reviewed source.
When Cosign is installed, the script also verifies the official Sigstore bundle against
the exact v0.73.0 release workflow identity and GitHub OIDC issuer. Signature failure
stops the scan. Inventory records distinguish checksum-only verification from checksum
plus Sigstore verification.

The compromised Trivy 0.69.4 release and mutable scanner/action tags are excluded.
See the [official incident advisory](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23),
[Trivy installation documentation](https://trivy.dev/docs/v0.73/getting-started/installation/),
and [release checksums](https://github.com/aquasecurity/trivy/releases/download/v0.73.0/trivy_0.73.0_checksums.txt).
The signature command follows the [official signature documentation](https://trivy.dev/docs/v0.73/getting-started/signature-verification/).

The manual `Public image vulnerability scan` workflow builds the project images from
public source and retains reports for fourteen days. Dependabot checks GitHub Actions,
the uv dependency manifests and Dockerfiles weekly. Local fixtures prove policy and
failure behavior; actual image scan results remain unknown until the script runs with
Docker and the vulnerability database available.
