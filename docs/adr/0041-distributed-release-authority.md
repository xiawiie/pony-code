# ADR-0041：分布式 Sandbox release authority 合同

- 状态：Accepted contract；Product Enablement 为 `NO-GO`
- 日期：2026-07-15

## 背景

本机 exact-image 能力不能证明一个 wheel、镜像、策略和跨平台证据已由发布方共同批准。未来的 distributed
Product Enablement 需要独立于 wheel 的签名 authority，并且在下载、缓存、回滚和重放边界上 fail closed。

## 决策

保留并测试现有 `sandbox_release_authority.py` 合同：

- 签名算法固定为 RSA-PSS-SHA256，3072-bit key、e=65537、32-byte salt。
- payload 使用 canonical ASCII JSON、domain separation、严格字段和 bounded no-follow reader。
- Product channel 固定为 stable GitHub Releases HTTPS，禁用代理，redirect 仅允许固定 authority，响应上限
  256 KiB。
- production public key map 必须不可变地进入 wheel，并支持 sequence、expiry、rotation、revocation 和 rollback
  拒绝。
- cache 固定为 `~/.pico/releases/docker-sandbox/product-enablement.json`，必须 owner-only、no-follow、原子写入。
- Candidate attestation 只供 release controller 的 nonce-bound public smoke，不可下载、持久缓存或升级成
  Product Enablement。
- runtime 重算 installed distribution、canonical image set 和 policy；wheel/sdist/commit/corpus/aggregate 是签名前
  核验并由签名认证的 provenance claims，不能从普通安装目录反推出。

## 当前判定

v0.2.0 不启用 distributed Product Enablement。production key map、KMS signing authority、registry-backed
arm64/amd64 image、四平台 mandatory/soak artifacts 和 detached Product Enablement 均不存在。缺少其中任何一项时，
distributed 入口必须在 target 启动前拒绝，不得把本机结果、contract tests 或空 aggregate 当作发布证据。

## 重新开启条件

只有新的独立发布工作完成 registry、KMS、双架构镜像、expected matrix、真实四平台 vertical、candidate smoke、
密钥轮换演练和 release review 后，才可重新评估。该工作不得通过修改 v0.2.0 本机授权或静默写入 cache 绕过。
