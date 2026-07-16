# Third-party notices

OmicsANG redistributes the following browser assets. They remain under their own licenses; the OmicsANG project license does not replace those terms.

## xterm.js 5.3.0

- Use: browser terminal component, vendored and redistributed.
- Official package: `https://registry.npmjs.org/xterm/-/xterm-5.3.0.tgz`
- Package SHA-256: `1f528168b828af1e05070a2e8639e96f96c50b5b57029215aac4dbed5410ab85`
- `xterm.js` SHA-256: `f0aea0f75f48559013ae6643c2479dd737d26da42d5524e6d2b70915ae6523c7`
- `xterm.css` SHA-256: `832f3f2c603b43ad4351ff04970150cc7a873014276db126a6065c6dd81e4872`
- License: MIT; exact upstream text is in `benchtop/web/vendor/xterm/5.3.0/LICENSE`.

## xterm-addon-fit 0.8.0

- Use: browser terminal sizing add-on, vendored and redistributed.
- Official package: `https://registry.npmjs.org/xterm-addon-fit/-/xterm-addon-fit-0.8.0.tgz`
- Package SHA-256: `93c401cc23d0632e47c267548258ae8d17742f8575e95d67022fd8760fc8c7e2`
- `xterm-addon-fit.js` SHA-256: `10f3194c5f17c1786fb7d5db865c1ec8539b6736a318063fd38bdaaf7c46848f`
- License: MIT; exact upstream text is in `benchtop/web/vendor/xterm-addon-fit/0.8.0/LICENSE`.

The JavaScript and CSS files above match their official package distributions byte-for-byte. Source-map files are not redistributed.

## Optional external executables

OmicsANG can detect or invoke separately installed tools including Conda, Mamba, Micromamba, Graphviz, Snakemake, Git, GitHub CLI, NCBI SRA Toolkit (`prefetch` and `fasterq-dump`), `pigz`, `gzip`, Claude Code, Codex, and the user's shell. These executables are not bundled or redistributed by OmicsANG. Users are responsible for installing them and complying with their providers' licenses, account requirements, and terms. Product names and trademarks belong to their respective owners; mention does not imply endorsement, partnership, sponsorship, or certification.

OmicsANG's optional SRA/GEO search calls the NCBI E-utilities HTTPS service at runtime; that service is contacted rather than redistributed. Search terms, selected databases, and accession identifiers are sent to NCBI. SRA Toolkit performs its own network downloads when the user explicitly starts a download job.
