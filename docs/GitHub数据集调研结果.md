# GitHub 数据集/体检单图片调研结果

> 2026-10-17　结论先行：**GitHub 上不存在现成的中文"体检报告"图片数据集**（这与 `tests/ocr_bench/README.md` 此前的调研一致）。但挖到了 8 张真实报告图，已放进 `tests/ocr_bench/samples/`。

---

## 一、找到的真图（已入库，git 不可见）

### 1. `csxiaoyaojianxian/BloodTestReportOCR`（★35，Apache-2.0）

https://github.com/csxiaoyaojianxian/BloodTestReportOCR

- 已下载 7 张真实血常规报告拍照件 → `samples/github_bloodtest_1~7.jpg`
- 附带 `bloodtestdata.json`（血常规 schema 模板，值全为 0，是模板不是标注）和
  `Spark/BloodTestReportDeepLearning/data_set.csv`（约 3000 行血常规数值 + 年龄性别，编码有乱码但可清洗）
- **图片特点（正是你合成样本缺的"最差情况"）**：
  - 真实纸张、手机拍照、**整体旋转 90°**、纸张边缘和装订线入镜、透视变形
  - 你的 synth.py 只做了 ±4° 旋转 + 噪点，没测过 90° 整转和真实透视——这 7 张直接补上
- **两个必须知道的坑**：
  1. ⚠️ **含真实 PII**（姓名"孟丁"、门诊号 90051065、样本号、医院名、检验者工号）。
     `samples/` 已被 gitignore 不会进仓库，但**绝不要把未脱敏版本 commit 或发出去**；
     标注 expected/*.json 时只写指标数值。
  2. ⚠️ **这是儿科报告（2 岁儿童）**，参考范围是儿童标准（WBC 4-10 vs 成人 3.5-9.5）。
     → **拿它测 OCR 和解析器**（版式抽取不分年龄），
     **不要拿它测红线判定**——用成人字典判儿童值会全线误报，
     这恰好也验证了你 README"不支持儿童"那条边界声明是真实的。
- 7 张 hash 各不相同，但 r1/r4 已确认是**同一份报告的不同拍摄角度**——
  这不是坏事：同版式不同退化 = 免费的鲁棒性测试组，ground truth 标一份即可复用。

### 2. `tommyMessi/Medical-table`（★9）

https://github.com/tommyMessi/Medical-table

- 1 张真实 尿干化学 + 镜检 + 乙肝两对半 报告扫描件 → `samples/github_urine_hepb_redacted.jpeg`
- **已经脱敏**（姓名/医院/检验者均涂黑），是唯一可以直接当"公开合规样本"用的
- 版式与血常规完全不同（双栏对照表 + 定性结果"-/+/2+"）——补充版式多样性
- 定性项目（阴性/阳性）不在你的字典范围内，测 OCR 层即可

### 3. `Mrduan-cloud/MediRead`（★2）— 无图价值

`tests/fixtures/sample_report.png` 是**明确标注 MOCK 的干净样图**（6 项指标），
信息量不如你自己的 synth.py，跳过。但这个项目本身是**同赛道竞品**（体检报告 OCR + RAG + Agent），
可以看它的交互设计。

---

## 二、搜过但没有结果的

| 检索词 | 结果 |
|---|---|
| 体检报告 数据集 / 化验单 数据集 / medical report OCR dataset Chinese | **0** |
| `guoyalun/Hostpital-Test-OCR`（★13，简介写"化验单OCR识别数据"） | 仓库里**没有图**，只有 7 个代码文件，名不副实 |
| `WakakaJu/Test-sheet_OCR`（★10） | 同样无图 |
| `13PG/physical_examination_analysis`（★4） | 只处理 PDF，无样本 |
| `zhangjiatao/Hospital-Report-Demo`（★12） | 只有一张界面截图 |
| `necusjz/np2016`（★18） | 与 BloodTestReportOCR 同源（中科大课程项目），同样的 7 张图 |

## 三、付费/许可受限的（维持你原判）

| 来源 | 规模 | 问题 |
|---|---|---|
| 数据堂 | 6087 张中文体检报告 | 商用付费，学生项目不值 |
| 天池/启智 中文化验单 | 238 张中文 | CC-BY-**NC**，你的合规线是"免费+可商用"，不碰 |
| ClinOCR-Bench / MedOCR-Vision | 384 / 2462 | MIT 但全英文 |

---

## 四、下一步怎么用这 8 张（配合《真实体检单怎么用.md》）

1. **标注优先级**：`github_urine_hepb_redacted.jpeg`（已脱敏，可放心细看）>
   `github_bloodtest_1.jpg`（清晰度最好）> 其余旋转件
2. **旋转件先转正再标注**，但**保留原旋转版本进 bench**——"OCR 能不能扛住 90° 整转"
   本身就是一个该写进 README 的发现
3. 血常规 7 张只需标**一份** ground truth（同版式），其余作为退化变体复用
4. 儿科参考范围差异正好用来验证**字典的 male/female/default 分层结构够不够**——
   如果以后想支持儿童，需要加 `age_group` 维度，这是个可以写进"未来工作"的点
5. 预期结果：RapidOCR 在这批真实件上的分数会**显著低于**合成件的 82%——
   把真实分数如实写进 README 的"已知限制"，这就是评审要的诚实

---

## 五、意外收获：三个同题竞品（可看交互，不可抄代码）

| 仓库 | 与你的差异 |
|---|---|
| `yuluo554/10-16-2-checkup-ledger` | 体检台账，历年报告纵向趋势——**和你的多报告对比撞车**，看它怎么呈现 |
| `njfufu2026/medical-report-structuring-demo` | 参考范围归一化 + 质控规则引擎（脱敏 demo），质控思路可借鉴 |
| `witzhang/medical-report-interpretation` | "每个数字可追回原页"——**和你的数字回溯校验撞车**，说明这个方向评委认可 |

> 官方明令禁止包装开源项目：只看交互与文档写法，代码一行别搬。
