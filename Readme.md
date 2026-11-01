# CTF AI Harness

Phase 0 provides local HexStrike execution proofs and provider tool-call
prototypes. The full application is planned in [TODO.md](TODO.md).

- [Integration findings, measured results, and commands](docs/integration-findings.md)
- [Provider and HexStrike configuration example](deploy/phase0.env.example)
- [Successful local assessment evidence](docs/evidence/phase0-report.json)

HexStrike uses port **8888**. The assessment runs harmless local fixtures only.
Provider fixtures pass; live compatibility is assumed until credentials exist.
Docker-to-host HTTP passed with Linux host networking. CTF/VPN callbacks remain
unverified; Docker Compose is not installed.

```sh
python3 -m unittest discover -s tests -v
python3 -m phase0.providers
```
