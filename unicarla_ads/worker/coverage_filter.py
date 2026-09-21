"""过滤 lcov tracefile 中编译器插入的异常分支。

GCC 的 -fprofile-arcs 会给可能抛异常的调用/赋值各插一条 fallthrough 边和一条
throw 边（.info 中 throw 边的块号带 e 前缀，紧跟在 fallthrough 边之后）。本脚本
成对删除这两条边，只保留源码里真实的条件分支；同时删掉 BRF/BRH 汇总行，让
lcov/genhtml 重新统计。

lcov 2.0 自带的 --filter branch / geninfo_no_exception_branch 会把同行真实
分支一起删掉，因此这里自行过滤。用法：
    python3 coverage_filter.py extracted.info coverage.info
"""

import sys


def filter_tracefile(src_path, dst_path):
    with open(src_path, encoding="utf-8", errors="replace") as src:
        content = src.readlines()

    with open(dst_path, "w", encoding="utf-8") as dst:
        block = []

        def flush():
            if not block:
                return
            brda = []  # [(原始行, 源文件行号, 是否异常边)]
            for raw in block:
                if raw.startswith("BRDA:"):
                    line, block_id = raw.strip().split(":", 1)[1].split(",")[:2]
                    brda.append((raw, line, block_id.startswith("e")))
            drop = [False] * len(brda)
            for i, (_, line, is_exception) in enumerate(brda):
                if not is_exception:
                    continue
                drop[i] = True
                # throw 边的前一条即同一行的 fallthrough 边，一并删除
                if i > 0 and brda[i - 1][1] == line and not brda[i - 1][2]:
                    drop[i - 1] = True

            index = 0
            for raw in block:
                if raw.startswith(("BRF:", "BRH:")):
                    continue
                if raw.startswith("BRDA:"):
                    if drop[index]:
                        index += 1
                        continue
                    index += 1
                dst.write(raw)

        for raw in content:
            if raw.startswith("SF:") and block:
                flush()
                block = []
            block.append(raw)
        flush()


def main():
    if len(sys.argv) != 3:
        raise SystemExit("用法: coverage_filter.py <输入.info> <输出.info>")
    filter_tracefile(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
