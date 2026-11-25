# musictidy

音乐库整理工具：**识别 → 写标签 → 归档 → 导出歌词封面**，一条流水线跑完。带图形界面，也支持命令行。

针对的痛点：网上下载的音乐库标签大面积缺失或被广告污染、目录结构混乱、同一首歌存了多个音质版本——「读标签自动整理」类软件在这类库上只会输出垃圾。musictidy 反过来做：先联网把元数据补对，再按模板归档。

## 功能

| 步骤 | 做什么 | 说明 |
|---|---|---|
| 扫描 | 收集音频文件 | flac / mp3 / wav / ape / m4a / wma / aac / ogg / dff / dsf |
| 识别 | 联网匹配元数据 | 文件名解析 → 按专辑分组查询 → 曲序/年份/专辑/封面；命中不了退回单曲搜索 |
| 写标签 | 写入六项 + 封面 + 歌词 | 曲名/歌手/专辑/年份/曲序/封面嵌入标签，歌词取 LRC；支持 dry-run 预览和断点续写 |
| 归档 | 按模板收拢目录 | `歌手\年份 - 专辑\NN 曲名.ext`；同录音冗余自动折叠，同名不同录音加 (2) |
| 导出 | 外置歌词与封面 | 每个音频旁同名 `.lrc`，每个专辑目录 `cover.jpg` + `folder.jpg` |

## 安装

**免安装（Windows）**：到 [Releases](https://github.com/szjmhxd/musictidy/releases) 下载 `musictidy.exe`，双击即用（ffmpeg/fpcalc 同样按下方说明配置）。

**从源码运行**：

```bash
git clone https://github.com/szjmhxd/musictidy.git
cd musictidy
pip install -r requirements.txt
```

可选（音频指纹去重用）：

- [ffmpeg](https://ffmpeg.org/) 在 PATH 里，或设环境变量 `FFMPEG`
- [fpcalc](https://acoustid.org/chromaprint)（Chromaprint 官方版）在 PATH 里，或设 `FPCALC`

## 使用

### 图形界面

```bash
python -m musictidy
# 或
python -m musictidy gui
```

流程：选目录 → 1.扫描 → 2.识别 → 3b.写入预览 → 3.写入标签 → 4.归档计划 → 4b.执行归档 → 5.导出歌词/封面。每步结果在表格里可见，日志实时滚动。

### 命令行

```bash
# 识别（联网，按专辑分组查询，比逐首搜索快得多）
python -m musictidy match --root "D:\Music" --workers 8 --out name.json

# 写标签（先预览再动手）
python -m musictidy write --from name.json --dry-run
python -m musictidy write --from name.json --resume

# 归档（先看计划再执行）
python -m musictidy organize --from name.json --root "D:\Music"
python -m musictidy organize --from name.json --root "D:\Music" --apply

# 导出外置歌词/封面
python -m musictidy export --root "D:\Music"
```

## 音源与合规

- 识别走公开音乐接口（网易云 / QQ 音乐 / 酷狗），按歌手自动路由；周杰伦等无网易云版权的歌手强制走 QQ。
- **仅供个人整理本地已有音频的元数据**，不提供下载能力。请遵守所在地法律法规与各平台服务条款。

## 风险提示

- 写标签会**修改音频文件**（只改标签字段，不动音频流；WAV 追加 ID3 chunk，不改 data 块）。先跑 dry-run 预览，重要库先备份。
- 归档移动是同卷 rename，速度快，但**目标已存在的文件会跳过**，执行前先看计划。
- 识别结果来自公开接口的搜索匹配，华语库版本混乱（同专辑有 Remastered / 日版 / 现场版），**曲序字段可信度低于曲名**，关键库建议抽查。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MTCLI_CACHE` | `~/.mtcli_cache` | 专辑/封面/歌词缓存目录（建议本地盘） |
| `FFMPEG` / `FPCALC` | PATH 查找 | 音频指纹工具路径 |
| `MTCLI_QPS` | `2.5` | 对接口的请求速率上限 |
| `MTCLI_ROOT` | `M:\_整理` | export 命令默认扫描目录 |

## 自行打包 exe

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name musictidy --collect-all zhconv run_gui.py
# 产物在 dist/musictidy.exe；`dist\musictidy.exe --smoke` 可做无界面自检
```

## 致谢

音源接口思路参考 [leafxdd/musictag-recovered](https://github.com/leafxdd/musictag-recovered)（MIT License）。

## License

MIT
