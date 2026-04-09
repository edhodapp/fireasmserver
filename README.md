# fireasmserver

A bare-metal x86_64 HTTP server that boots directly as a [Firecracker](https://firecracker-microvm.github.io/) microVM guest. No Linux kernel underneath, no userspace, no runtime — the kernel image *is* the HTTP server.

`fireasmserver` is the first product in the **fireasm** family of bare-metal Firecracker-hosted services. Downstream of [ws_pi5](https://github.com/edhodapp/ws_pi5)'s protocol stack, retargeted from AArch64 + GENET to x86_64 + virtio-net.

## Status

Design phase. No source yet. This repository exists to claim the name and pin the license.

## License

fireasmserver is licensed under the **GNU Affero General Public License, version 3 or any later version** (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the full text.

If your use case is incompatible with the AGPL, contact ed@hodapp.com about commercial licensing.

Copyright © 2026 Ed Hodapp.

## Contributions

Bug reports welcome. Pull requests are not accepted — fireasmserver is a single-author project. If you have a fix, file an issue and it may be reimplemented.

## Acknowledgements

Thanks to Chetan Venkatesh for pointing at Firecracker as the right deployment target for this kind of work.
