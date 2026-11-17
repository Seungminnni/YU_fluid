# YU_fluid License

**Copyright © 2026 Seungmin Lee and Heonje Woo. All rights reserved.**

## 1. Original Materials and Copyright

This license applies to the portions of the YU_fluid source code, scripts, configurations, documentation, CAD drawings and models, viewer assets, and other designated handover materials for which Seungmin Lee and Heonje Woo (the "Copyright Holders") hold copyright (the "Original Materials"). The repository baseline at adoption of this license is commit `52c95eadb34e33c1a89f2817b3df3454cbb37e49`.

The Copyright Holders retain copyright in the Original Materials. Copies and modified versions containing any of the Original Materials must preserve the original copyright and applicable license notices. All rights in the Original Materials are reserved except as expressly granted under this license or applicable law.

## 2. Internal Laboratory Collaboration

A laboratory member receives a nonexclusive license to work on YU_fluid for internal, noncommercial purposes by accepting an invitation to the private repository approved by both Copyright Holders and agreeing to these terms. This grant covers the following activities within the approved scope of collaboration.

1. Access and review the handover materials, clone the repository locally, and install, run, test, and verify the project.
2. Modify and adapt the Original Materials, fix issues, and add features for internal noncommercial research, maintenance, and joint development of YU_fluid.
3. Work on branches in the private repository, submit pull requests, and review and merge changes with authorized collaborators as permitted by the member's repository permissions.

The grant ends when the agreed period expires, the collaboration ends, or repository access is revoked, whichever comes first. Any separate permission under Section 3 remains subject to its own terms.

Copyright in each new contribution belongs to its applicable rights holder under applicable law and any separate agreement. By submitting a contribution for inclusion, the contributor grants the Copyright Holders and authorized collaborators a nonexclusive license to review, merge, copy, run, and modify that contribution within the private YU_fluid project for its internal operation, maintenance, and further development. This internal license continues after the contributor's participation ends.

## 3. Uses Requiring Separate Prior Written Permission

A collaborator must obtain separate prior written permission from **both Copyright Holders** before doing any of the following with the Original Materials or a modified version containing them.

1. Use, modify, or adapt them outside the approved scope or after the collaboration ends, or incorporate them into another repository, product, or project.
2. Give anyone outside the authorized collaboration access to files or the repository, publish the materials to an external repository or service, or redistribute them.
3. Use the materials commercially, including for sale, paid services, client or contract work, or commercial product development or testing.
4. Transfer or sublicense the rights granted under this license.

External sharing and commercial use require this permission during and after the collaboration. If the proposed use includes a contribution owned by another party, obtain any required permission from that rights holder as well.

## 4. OpenFOAM and Third-Party Materials

OpenFOAM, GPL-covered solver modifications and associated materials, and other third-party components are governed by their respective upstream licenses and notices. The permission requirements for the Original Materials apply to the project-owned portions of copies and modified versions. Recipients may exercise the rights granted by the applicable third-party licenses for their respective components, including any rights to modify, redistribute, or use those components commercially. Use of the Yeungnam University emblem is subject to the applicable rights holder's terms.

The project uses the OpenCFD OpenFOAM v2512 base image `opencfd/openfoam-default:2512`. It uses `src/cfd_twin/patch_adaptive_cht_solver.py` to modify the OpenFOAM `chtMultiRegionFoam` solver and builds `chtMultiRegionFoamAdaptive`. When conveying GPL-covered source code, the modified solver binary, or a container image containing it, preserve upstream notices, include the GPL text, and identify modifications and their dates as required by the GNU GPL. When conveying binaries, also make the Corresponding Source available as required by GPL Sections 5 and 6. This includes the matching upstream source, the changes, and the build and installation scripts required by the GPL.

[OpenFOAM v2512 source](https://dl.openfoam.com/source/v2512/) · [GNU GPL v3](https://www.gnu.org/licenses/gpl-3.0.html)

## Third-Party License References

The table lists direct dependencies declared in `pyproject.toml` and the documented runtime environment. For any distributed package or image, provide the notices and license texts required for the versions and additional components it actually includes.

| Component | Role | License and upstream references |
| --- | --- | --- |
| OpenCFD OpenFOAM v2512 | CFD computation and modified CHT solver | GNU GPL v3, subject to the notices in the upstream source files. [Upstream license](https://www.openfoam.com/licensing) |
| PyYAML 6.0 or later | YAML configuration | MIT. [Upstream license](https://github.com/yaml/pyyaml/blob/main/LICENSE) |
| Pillow 10 or later | Image processing | See the license shipped with the installed version. [Pillow 10.0.0, HPND](https://github.com/python-pillow/Pillow/blob/10.0.0/LICENSE) · [Current upstream version, MIT-CMU](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| CadQuery 2.5 or later | CAD generation and processing | Apache License 2.0. [Upstream license](https://github.com/CadQuery/cadquery/blob/master/LICENSE) |
| NumPy 2.0 or later | Numerical processing of simulation results | BSD 3-Clause. [Upstream license](https://numpy.org/doc/stable/license.html) |
| Matplotlib 3.9 or later | Charts and drawings | PSF-based Matplotlib license. [Upstream license and additional notices](https://matplotlib.org/stable/project/license.html) |
| VTK 9.3 or later | Mesh and simulation result processing | BSD 3-Clause. [Upstream license](https://github.com/Kitware/VTK/blob/master/Copyright.txt) |
| setuptools 68 or later | Python package build | MIT. [Upstream license](https://github.com/pypa/setuptools/blob/main/LICENSE) |
| Python 3.12 or later | Runtime | PSF license and additional distribution notices. [Upstream license](https://docs.python.org/3/license.html) |
| Docker Desktop | Documented container runtime | Docker Subscription Service Agreement. [Docker Desktop terms](https://docs.docker.com/subscription-billing/desktop-license/) |
