# Zipatch V3 修改包协议规范

当前版本：V3.0

本文档规定 AI 构建 Zipatch V3 修改包时必须遵守的格式、操作类型、路径规则、内容定位规则和输出自检规则。

协议名固定为：

```text
ZIPATCH_V3
```

整个修改包必须放在一个 Markdown `text` 代码块中，确保用户可以一次性完整复制。

外层代码块围栏长度必须大于修改包正文中出现的任何连续反引号长度。默认使用至少四个反引号；如果正文包含同等长度的连续反引号，必须继续加长外层围栏。

---

## 1. 基本结构

```text
<<ZIPATCH_V3 boundary="ZIPATCH_BOUNDARY_唯一边界字符串">>
---OP 操作类型 id="唯一OP标识" 结构参数="..."
---PATH
相对路径
ZIPATCH_BOUNDARY_唯一边界字符串
---CONTENT
正文内容
ZIPATCH_BOUNDARY_唯一边界字符串
---END_OP
<</ZIPATCH_V3>>
```

基本要求：

1. 首个非空行必须是包头，最后非空行必须是包尾；
2. 包头和包尾必须独占一行，其前后不得出现其他非空内容；
3. 一个修改包可以包含一个或多个 OP，按书写顺序排列；
4. 每个 OP 以 `---OP ...` 开始，以 `---END_OP` 结束；
5. 每个 OP 必须声明唯一的 `id`；
6. 路径、目标路径、源码锚点和正文原文必须使用协议规定的文本块；
7. 每个文本块都由本包统一的 `boundary` 独占一行结束；
8. 文本块内容按原文读取，不使用字符串转义；
9. OP 头只用于声明 `id`、整数和枚举等结构参数；
10. 每个 OP 只能使用该操作定义中允许的头参数和文本块；
11. Markdown 围栏不属于修改包载荷，必须与包头、包尾分别独占一行。

包头：

```text
<<ZIPATCH_V3 boundary="ZIPATCH_BOUNDARY_...">>
```

包尾：

```text
<</ZIPATCH_V3>>
```

合法文本块：

| 文本块 | 用途 |
| --- | --- |
| `---PATH` | OP 的主相对路径 |
| `---NEW_PATH` | 双路径 OP 的目标相对路径 |
| `---START_MARKER` | `replace_between` 的起始源码锚点 |
| `---END_MARKER` | `replace_between` 的结束源码锚点 |
| `---CONTENT` | 完整文件、追加内容或替换后的完整区间 |
| `---OLD` | `replace_exact` 的旧文本 |
| `---NEW` | `replace_exact` 的新文本 |

---

## 2. boundary

`boundary` 是修改包内所有文本块共用的结束标记。

要求：

1. 必须以 `ZIPATCH_BOUNDARY_` 开头；
2. 长度至少为 32 个字符，建议不超过 160 个字符；
3. 只能包含字母、数字、下划线、短横线和点；
4. 必须包含 14 位创建时间码 `YYYYMMDDHHMMSS`；
5. 不得等于协议关键字；
6. 不得在任一文本块正文中作为独立一行出现；
7. 同一修改包内的所有文本块必须使用同一个 boundary；
8. boundary 只负责结束文本块，不参与内容转义；
9. 文本块中的引号、反斜杠、等号、空格和源码符号均按原文处理。

示例：

```text
ZIPATCH_BOUNDARY_TASK_20260930164800_A1B2C3D4E5F6
```

---

## 3. 路径规则

所有 `PATH` 和 `NEW_PATH` 都必须是相对执行器当前所选项目根目录的路径。

主路径：

```text
---PATH
src/main.py
ZIPATCH_BOUNDARY_...
```

双路径 OP 的目标路径：

```text
---NEW_PATH
src/new_main.py
ZIPATCH_BOUNDARY_...
```

示例：

```text
允许：
README.md
docs/spec.md
src/main.py
70_后台文件/10_知识资产/素材.md

禁止：
E:\Project\README.md
C:/Project/README.md
/Users/name/project/README.md
../README.md
docs/*.md
src/file?.py
```

要求：

1. 每个 OP 必须包含且只能包含一个非空 `PATH`；
2. 双路径 OP 必须额外包含且只能包含一个非空 `NEW_PATH`；
3. 路径直接写入文本块，不使用引号包裹或字符串转义；
4. 路径不得使用绝对路径、Windows 盘符、`..`、通配符或空值；
5. 路径不得逃逸项目根目录；
6. 推荐统一使用 `/` 作为路径分隔符；
7. `delete_dir`、`rename_dir`、`move_dir`、`copy_dir` 和 `create_dir` 不得作用于项目根目录；
8. 修改包不得重命名、移动、删除或改变项目根目录本身；
9. 如需修改项目根目录名称，应先完成根目录内部文件修改，再由用户手动重命名；
10. 所有路径必须按执行时的当前项目根目录计算。

---

## 4. OP 分类与结构

### 4.1 OP 分类

单路径 OP：

- `write_file`
- `append_text`
- `replace_exact`
- `replace_between`
- `delete_file`
- `delete_dir`
- `create_dir`

双路径 OP：

- `rename_file`
- `move_file`
- `copy_file`
- `rename_dir`
- `move_dir`
- `copy_dir`

内容 OP：

- `append_text`
- `replace_exact`
- `replace_between`

目标类 OP：

- `write_file`
- `rename_file`
- `move_file`
- `copy_file`
- `rename_dir`
- `move_dir`
- `copy_dir`

### 4.2 OP 总览

| OP | 用途 |
| --- | --- |
| `write_file` | 写入完整文件 |
| `append_text` | 向已有文件末尾追加文本 |
| `replace_exact` | 精确替换旧文本 |
| `replace_between` | 替换两个锚点之间的完整区间 |
| `delete_file` | 删除单个文件 |
| `delete_dir` | 删除整个文件夹 |
| `rename_file` | 在同一目录内重命名文件 |
| `move_file` | 移动文件，可跨目录或同时改名 |
| `copy_file` | 复制文件 |
| `rename_dir` | 在同一父目录内重命名文件夹 |
| `move_dir` | 移动文件夹，可跨目录或同时改名 |
| `copy_dir` | 复制文件夹 |
| `create_dir` | 创建文件夹 |

### 4.3 OP 唯一标识 id

每个 OP 必须在 `---OP` 行声明 `id`，用于日志、错误提示和人工定位。

通用格式：

```text
---OP 操作类型 id="唯一OP标识" 结构参数="..."
```

示例：

```text
---OP replace_exact id="op018_fix_title" count="1"
```

要求：

1. `id` 必填且不得为空；
2. 同一修改包内所有 `id` 必须唯一；
3. `id` 只能包含字母、数字、下划线和短横线；
4. 建议长度为 3 至 80 个字符；
5. 推荐使用递增编号，如 `op001`、`op002`、`op003`；
6. 复杂修改包可以使用编号加语义后缀，如 `op018_fix_title`；
7. 校验或执行失败时，错误信息必须包含相关 OP 的 `id`。

### 4.4 OP 头参数

各 OP 允许的头参数如下：

| OP | 允许的头参数 |
| --- | --- |
| `write_file` | `id`、`if_exists` |
| `append_text` | `id` |
| `replace_exact` | `id`、`count` |
| `replace_between` | `id` |
| `delete_file` | `id` |
| `delete_dir` | `id` |
| `rename_file` | `id`、`if_exists` |
| `move_file` | `id`、`if_exists` |
| `copy_file` | `id`、`if_exists` |
| `rename_dir` | `id`、`if_exists` |
| `move_dir` | `id`、`if_exists` |
| `copy_dir` | `id`、`if_exists` |
| `create_dir` | `id`、`if_exists` |

路径、目标路径和源码锚点必须使用对应文本块，不得写入 OP 头。

程序必须拒绝重复参数、未知参数和当前 OP 不允许的参数。

---

## 5. 全局互斥规则

### 5.1 路径互斥

各 OP 的相关路径如下：

| OP | 相关路径 |
| --- | --- |
| 单路径 OP | `PATH` |
| 双路径 OP | `PATH`、`NEW_PATH` |

路径互斥表示两个相关路径不得：

- 完全相同；
- 形成父子包含关系。

同一修改包内，除两个内容 OP 之间外，任意两个 OP 的相关路径都必须互斥。

双路径 OP 自身的 `PATH` 与 `NEW_PATH` 也必须互斥。

禁止示例：

```text
---OP delete_file id="op001"
---PATH
a.md
ZIPATCH_BOUNDARY_...
---END_OP
---OP copy_file id="op002" if_exists="overwrite"
---PATH
template.md
ZIPATCH_BOUNDARY_...
---NEW_PATH
a.md
ZIPATCH_BOUNDARY_...
---END_OP
```

如果需要用模板整体覆盖目标，应使用单个目标类 OP：

```text
---OP copy_file id="op001" if_exists="overwrite"
---PATH
template.md
ZIPATCH_BOUNDARY_...
---NEW_PATH
a.md
ZIPATCH_BOUNDARY_...
---END_OP
```

### 5.2 内容互斥

内容互斥适用于同一文件内的 `replace_exact` 和 `replace_between`。

规则：

1. 每个替换 OP 必须先定位到执行前源文件中的旧文本区间；
2. 同一文件内任意两个旧文本位置区间不得重叠；
3. 比较的是旧文本在文件中的实际位置，不是 OLD 字符串是否相同；
4. `replace_between` 的区间包含起始和结束 marker；
5. 两个相邻区间不得共享同一个 marker；
6. `append_text` 不参与内容互斥，多个 `append_text` 可以按 OP 顺序追加；
7. 所有 OP 都必须基于执行前的同一份文件内容独立校验；
8. 后续 OP 不得依赖前序 OP 修改后的中间结果；
9. 连续或相互影响的修改区域应合并为一个更大的替换 OP，或使用 `write_file`。

---

## 6. if_exists

目标类 OP 支持：

```text
if_exists="error|overwrite|skip"
```

默认值为 `error`。

`create_dir` 支持：

```text
if_exists="error|skip"
```

默认值为 `skip`。

| 值 | 含义 |
| --- | --- |
| `error` | 目标已存在时失败 |
| `overwrite` | 目标已存在时整体替换目标 |
| `skip` | 目标已存在且类型匹配时跳过本 OP |

约束：

1. `overwrite` 表示整体替换，不执行内容合并；
2. 文件类 OP 只能覆盖文件；
3. 目录类 OP 只能覆盖目录；
4. 目录覆盖必须整体替换，不保留目标目录中的额外内容；
5. `skip` 只适用于目标已经存在且类型匹配的情况；
6. 源路径不存在、源类型不匹配或目标类型不匹配时必须失败。

---

## 7. 通用文本块规则

1. 每个 OP 都必须使用 `PATH`；
2. 双路径 OP 额外使用 `NEW_PATH`；
3. `write_file` 和 `append_text` 使用 `CONTENT`；
4. `replace_exact` 使用 `OLD` 和 `NEW`；
5. `replace_between` 使用 `START_MARKER`、`END_MARKER` 和 `CONTENT`；
6. `delete_file`、`delete_dir`、双路径 OP 和 `create_dir` 不使用正文内容块；
7. 每个文本块必须由包头声明的 boundary 独占一行结束；
8. 同一 OP 内不得重复出现同名文本块；
9. 不得使用当前 OP 未定义的文本块；
10. `PATH`、`NEW_PATH`、`START_MARKER`、`END_MARKER` 和 `OLD` 不得为空；
11. `CONTENT` 和 `NEW` 允许为空，以支持写入空文件、追加空文本或删除旧文本；
12. 文本块内容按原文处理，不对引号、反斜杠、等号或空格进行转义。

---

## 8. OP 定义

### 8.1 write_file

```text
---OP write_file id="op001" if_exists="error"
---PATH
相对路径
ZIPATCH_BOUNDARY_...
---CONTENT
完整文件内容
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. `PATH` 和 `CONTENT` 必填；
2. 目标不存在时创建文件；
3. 目标父目录不存在时自动创建；
4. 目标存在且是目录时失败；
5. `if_exists` 使用目标类通用规则。

### 8.2 append_text

```text
---OP append_text id="op001"
---PATH
相对路径
ZIPATCH_BOUNDARY_...
---CONTENT
追加内容
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. `PATH` 和 `CONTENT` 必填；
2. 目标必须是已存在的文件；
3. 内容固定追加到文件末尾。

### 8.3 replace_exact

```text
---OP replace_exact id="op001" count="1"
---PATH
相对路径
ZIPATCH_BOUNDARY_...
---OLD
旧文本
ZIPATCH_BOUNDARY_...
---NEW
新文本
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. 目标必须是已存在的文件；
2. `count` 必填且必须是大于等于 1 的整数；
3. `OLD` 必须与目标文件的当前文本精确匹配；
4. `OLD` 不得为空，`NEW` 允许为空；
5. 实际命中次数必须等于 `count`；
6. `count > 1` 表示替换全部命中的多处旧文本；
7. 同文件多个替换 OP 的旧文本位置区间不得重叠。

### 8.4 replace_between

```text
---OP replace_between id="op001"
---PATH
相对路径
ZIPATCH_BOUNDARY_...
---START_MARKER
起始源码锚点
ZIPATCH_BOUNDARY_...
---END_MARKER
结束源码锚点
ZIPATCH_BOUNDARY_...
---CONTENT
替换后的完整区间
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. 目标必须是已存在的文件；
2. `START_MARKER` 和 `END_MARKER` 必填且不得为空；
3. 两个 marker 在目标文件中都必须唯一；
4. 起始 marker 必须位于结束 marker 之前；
5. marker 按源码原文读取，可以包含引号、反斜杠、等号和多行文本；
6. 替换区间包含起始 marker 和结束 marker；
7. `CONTENT` 必须是替换后的完整区间；
8. 如需保留 marker，必须在 `CONTENT` 中显式写回。

### 8.5 delete_file

```text
---OP delete_file id="op001"
---PATH
相对文件路径
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. 目标必须是已存在的文件；
2. 不允许用于删除目录。

### 8.6 delete_dir

```text
---OP delete_dir id="op001"
---PATH
相对目录路径
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. 目标必须是已存在的目录；
2. 不允许删除项目根目录；
3. 执行时删除该目录及其全部子内容。

### 8.7 rename_file

```text
---OP rename_file id="op001" if_exists="error"
---PATH
docs/old.md
ZIPATCH_BOUNDARY_...
---NEW_PATH
docs/new.md
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. `PATH` 必须存在且是文件；
2. `PATH` 与 `NEW_PATH` 必须具有相同父目录；
3. `if_exists` 使用目标类通用规则。

### 8.8 move_file

```text
---OP move_file id="op001" if_exists="error"
---PATH
draft/a.md
ZIPATCH_BOUNDARY_...
---NEW_PATH
final/a.md
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. `PATH` 必须存在且是文件；
2. 允许跨目录移动和同时改名；
3. 目标父目录不存在时自动创建；
4. `if_exists` 使用目标类通用规则。

### 8.9 copy_file

```text
---OP copy_file id="op001" if_exists="error"
---PATH
template.md
ZIPATCH_BOUNDARY_...
---NEW_PATH
a.md
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. `PATH` 必须存在且是文件；
2. 源文件保持不变；
3. 目标父目录不存在时自动创建；
4. `if_exists` 使用目标类通用规则。

### 8.10 rename_dir

```text
---OP rename_dir id="op001" if_exists="error"
---PATH
docs/old
ZIPATCH_BOUNDARY_...
---NEW_PATH
docs/new
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. `PATH` 必须存在且是目录；
2. `PATH` 与 `NEW_PATH` 必须具有相同父目录；
3. 不允许作用于项目根目录；
4. `if_exists` 使用目标类通用规则。

### 8.11 move_dir

```text
---OP move_dir id="op001" if_exists="error"
---PATH
old/topic
ZIPATCH_BOUNDARY_...
---NEW_PATH
new/topic
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. `PATH` 必须存在且是目录；
2. 允许跨目录移动和同时改名；
3. 不允许作用于项目根目录；
4. 目标父目录不存在时自动创建；
5. `if_exists` 使用目标类通用规则。

### 8.12 copy_dir

```text
---OP copy_dir id="op001" if_exists="error"
---PATH
template_project
ZIPATCH_BOUNDARY_...
---NEW_PATH
project_a
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. `PATH` 必须存在且是目录；
2. 源目录保持不变；
3. 不允许复制项目根目录；
4. 目标父目录不存在时自动创建；
5. `if_exists` 使用目标类通用规则。

### 8.13 create_dir

```text
---OP create_dir id="op001" if_exists="skip"
---PATH
docs/new
ZIPATCH_BOUNDARY_...
---END_OP
```

规则：

1. 支持创建多级目录；
2. 不允许作用于项目根目录；
3. `if_exists` 仅支持 `error` 或 `skip`；
4. 路径已存在但不是目录时失败。

---

## 9. 锚点与旧文本

使用 `replace_exact` 或 `replace_between` 时，必须保证定位稳定且唯一。

要求：

1. 不使用 `---`、空行或常见短句作为 marker；
2. 优先使用稳定标题、函数定义、类定义或唯一上下文；
3. `OLD` 必须与执行前的文件原文精确匹配；
4. marker 可以包含引号、反斜杠、等号、空格和多行源码；
5. 起止 marker 必须在执行前原文中唯一且顺序正确；
6. 结束 marker 优先选择下一个同级标题或稳定结构标记；
7. 每个替换 OP 都必须能基于执行前文件独立定位；
8. 连续或相互影响的改动应合并为一个更大的替换 OP；
9. 定位不安全时，应扩大上下文、使用多行 marker、改用 `write_file`，或要求用户补充原文；
10. 文本块原文中的空格、引号和反斜杠不得被擅自删除。

---

## 10. 失败定位规则

执行器校验或执行某个 OP 失败时，必须输出该 OP 的 `id`。

失败信息应包含：

1. OP 的 `id`；
2. OP 类型；
3. 主要路径；
4. 失败原因；
5. 通过 `id="..."` 搜索修改包的定位提示。

推荐格式：

```text
[失败] id="op018" replace_exact path="ui/favorites.py"

失败原因：
replace_exact 命中次数不符：expected=1, actual=0

修改包定位：
请在修改包中搜索：
id="op018"
```

全局冲突应明确列出相关 OP 的 `id`，并禁止执行整个修改包。

---

## 11. 输出前自检

输出修改包前必须检查：

| 检查项 | 要求 |
| --- | --- |
| 输出载体 | 整包放入 Markdown `text` 代码块，外层围栏长度足够 |
| 协议 | 使用 `ZIPATCH_V3` |
| 包头与包尾 | 格式完整，独占一行，包尾后无非空内容 |
| boundary | 格式合法、全包统一、包含 14 位时间码 |
| boundary 冲突 | 不得在文本块正文中作为独立一行出现 |
| OP | 每个 OP 都有 `---OP` 和 `---END_OP` |
| OP id | 每个 OP 的 `id` 必填、唯一且格式合法 |
| OP 头 | 只包含当前 OP 允许的结构参数 |
| PATH | 每个 OP 有且只有一个非空 `PATH` |
| NEW_PATH | 双路径 OP 有且只有一个非空 `NEW_PATH` |
| marker | `replace_between` 使用非空且唯一的起止 marker |
| 文本块 | 不缺失、不重复、不使用当前 OP 不允许的文本块 |
| 文本块结束 | 每个文本块都由 boundary 独占一行结束 |
| 原文语义 | 路径、marker、OLD、NEW 和 CONTENT 均按原文表达 |
| 路径安全 | 使用相对路径，不含盘符、`..` 或通配符 |
| 根目录安全 | 不重命名、移动、复制或删除项目根目录 |
| 路径互斥 | 非内容 OP 的相关路径不得相同或形成父子关系 |
| 内容互斥 | 同文件替换 OP 的旧文本位置区间不得重叠 |
| 独立校验 | 后续 OP 不依赖前序 OP 的执行结果 |
| if_exists | 只使用当前 OP 支持的枚举值 |
| overwrite | 只表示整体替换，不表示内容合并 |
| replace_exact | 显式声明 `count`，OLD 精确命中指定次数 |
| replace_between | marker 唯一且顺序正确，CONTENT 是完整新区间 |
| 删除操作 | 只在需求明确要求删除时使用 |
| 失败定位 | 错误信息能够通过 OP `id` 快速定位 |
| 备份 | 修改包不声明备份路径，由执行器管理备份 |

---

## 12. 完整示例

```text
<<ZIPATCH_V3 boundary="ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6">>
---OP copy_file id="op001" if_exists="skip"
---PATH
templates/readme.md
ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6
---NEW_PATH
README.md
ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6
---END_OP
---OP replace_exact id="op002" count="1"
---PATH
docs/spec.md
ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6
---OLD
旧标题
ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6
---NEW
新标题
ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6
---END_OP
---OP replace_between id="op003"
---PATH
main.py
ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6
---START_MARKER
def run_gui_flow():
ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6
---END_MARKER
if __name__ == "__main__":
ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6
---CONTENT
def run_gui_flow():
    print("启动程序")


if __name__ == "__main__":
    run_gui_flow()
ZIPATCH_BOUNDARY_EXAMPLE_20260930164800_A1B2C3D4E5F6
---END_OP
<</ZIPATCH_V3>>
```
