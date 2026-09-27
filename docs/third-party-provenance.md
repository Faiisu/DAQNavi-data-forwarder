# Advantech software and hardware references

Advantech's SDK examples, bundled helper libraries, standalone polling example, and PCI-1716 manual have been removed from this repository. Obtain current vendor software, drivers, libraries, and manuals from Advantech's official pages:

- [DAQNavi Driver for Linux and SDK downloads](https://www.advantech.com/emt/support/details/driver?id=1-LXHFQJ)
- [DAQNavi SDK product information](https://www.advantech.com/en-us/products/DAQNavi-SDK/mod/E76C77D9-D049-48B6-8EBF-940C0F5F8632)
- [PCI-1716 Series User Manual](https://www.advantech.com/en-us/support/details/manual?id=1-7O1XO)

Install the DAQNavi driver and required libraries on the Linux host according to Advantech's current package instructions. The project does not redistribute vendor example code, SDK binaries, generated artifacts, or the manual. The Python binding dependency is declared in the project requirements and installed by the package manager; it is not vendored here.

[`hardware/PCI-1716.md`](hardware/PCI-1716.md) is a project-authored paraphrased engineering note. It links to the official manual and should be checked against that source before wiring or safety-critical use. The project does not claim ownership of Advantech materials or grant rights to redistribute them.
