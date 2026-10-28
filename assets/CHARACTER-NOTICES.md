# 大肥鱼角色规范与素材来源

角色规范参考 [梗鲸教程](https://aigengtu.com/tutorial) 和 [DeepSeek Whale-chan](https://github.com/Neko3000/deepseek-whalechan)（作者 Neko3000）。参考上游提交 `bb8182a0092004c4a9525254f96aeaf74874b9a9` 中的 `skills/whalechan-image-character/references/character-spec.md`。

本项目改编整理其固定身份、参考图约束和目视检查方法，固定现有服装，仅保留场景插画和 meme 两种比例，特别明确只有鲸鳍耳、没有人耳；不移植上游完整工具链、台词或行为风格。

上游规范文档及本项目由其改编的 `config/persona.toml` 中 `visual.prompt` 遵循 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)。本来源说明与 `docs/specs/character.md` 中的整理内容遵循相同许可。上述许可不自动覆盖项目其他代码或第三方图片。

上游列出的第三方形象来源包括上善无形、ZipZipPipe；图片权利范围见其 [第三方说明](https://github.com/Neko3000/deepseek-whalechan/blob/main/THIRD-PARTY-NOTICES.md)。本次角色图和 26 张 meme 由项目已有图片修订，没有复制上游参考图片到项目，也不据此推定第三方图片的授权范围。

具体生成提示词、资产路径与 SHA-256 见同目录的 `character-normalization.json`。本说明及该记录随源码发布包保留。
