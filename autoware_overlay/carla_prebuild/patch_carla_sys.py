"""离线构建时把 carla-sys 的下载逻辑替换为本地预编译包拷贝。"""
import sys

path = sys.argv[1]
text = open(path).read()
start = text.index("fn download_tarball()")
end = text.index("\n}\n", start) + 3
replacement = '''fn download_tarball() -> Result<Option<PathBuf>> {
    std::fs::copy(
        "/mnt/carla-prebuild/libcarla_client.0.9.16-x86_64-unknown-linux-gnu.tar.zstd",
        &*DOWNLOAD_PREBUILT_TARBALL,
    )?;
    Ok(Some(DOWNLOAD_PREBUILT_TARBALL.to_path_buf()))
}
'''
open(path, "w").write(text[:start] + replacement + text[end:])
print("patched", path)
