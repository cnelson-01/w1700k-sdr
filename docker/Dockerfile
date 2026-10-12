# Linux build environment for the unit images (Buildroot, Linux, U-Boot), for hosts
# that are not Linux. See docker/build-images.sh.
FROM debian:trixie
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential gcc g++ make git ca-certificates wget curl rsync bc cpio file unzip xz-utils \
        bzip2 zstd patch perl python3 python3-dev python3-setuptools python3-pyelftools \
        libncurses-dev libssl-dev openssl uuid-dev libgnutls28-dev swig flex bison \
        clang lld llvm libfftw3-dev zlib1g-dev device-tree-compiler kmod squashfs-tools \
        xxd sed gawk diffutils findutils which pkg-config && \
    rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 build && mkdir /work && chown build /work
USER build
WORKDIR /work
