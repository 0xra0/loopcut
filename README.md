Loopcut glog install fix v2

Previous script built only `--target blender`, but `cmake --install` also installs `blender-thumbnailer`. v2 builds all configured targets and checks for thumbnailer when enabled. It does not install automatically.

Quick recovery:

cd /mnt/sata/loopcut
cmake --build build-full-glogfix --parallel 2
test -x build-full-glogfix/bin/blender-thumbnailer
build-full-glogfix/bin/blender --help >/dev/null
sudo cmake --install build-full-glogfix
/usr/bin/blender --help >/dev/null

To replace your script, copy fix-loopcut-glog-v2.sh to /mnt/sata/loopcut/fix-loopcut-glog.sh. Bash syntax and patch checks have passed; a full HIP build has not been tested in this environment.
