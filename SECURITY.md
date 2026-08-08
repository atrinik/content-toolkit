# Security policy

Report vulnerabilities privately through GitHub's security advisory interface.
Do not include private content, credentials, or player data in a report.

All input is untrusted. Parsers and decoders must validate declared bounds
before allocation, remain deterministic, and return diagnostics without
partially mutating caller state. The CLI never discovers sibling projects or
overwrites an existing output path.
