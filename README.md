# KeenInsight

<div align="center">

**A Comprehensive System Intelligence & Performance Analytics Platform**

![Python](https://img.shields.io/badge/Python-59.7%25-blue)
![C++](https://img.shields.io/badge/C%2B%2B-20.6%25-brightgreen)
![Vue](https://img.shields.io/badge/Vue-8.8%25-4FC08D)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

</div>

## 📋 项目概述

KeenInsight 是一个强大的系统洞察与性能分析平台，整合了多种技术栈，提供深度的系统监测、性能优化和数据分析能力。项目采用前后端分离架构，结合高性能 C++ 模块实现数据处理。

## 🌟 核心特性

- **系统监测** - 实时监测系统运行状态和性能指标
- **性能分析** - 深度分析系统瓶颈和优化方向
- **数据可视化** - 基于 Vue 的交互式仪表板
- **高性���处理** - C++ 加速的数据处理模块
- **可扩展架构** - 模块化设计，易于扩展

## 🛠️ 技术栈

| 技术 | 占比 | 用途 |
|------|------|------|
| **Python** | 59.7% | 后端核心逻辑、数据处理 |
| **C++** | 20.6% | 高性能计算、系统级操作 |
| **Vue** | 8.8% | 前端界面、交互显示 |
| **TypeScript** | 2.5% | 类型安全的前端开发 |
| **Makefile** | 4.4% | 构建和编译管理 |
| **CMake** | 2.0% | C++ 项目构建 |

## 📦 项目结构

```
KeenInsight/
├── sysinsight_front/          # Vue 前端应用
├── backend/                   # Python 后端服务
├── core/                      # C++ 高性能模块
├── Makefile                   # 构建脚本
├── CMakeLists.txt            # CMake 构建配置
└── README.md                 # 项目文档
```

## 🚀 快速开始

### 前置要求

- Python 3.8+
- Node.js 16+ 与 npm
- C++ 编译器 (GCC/Clang)
- CMake 3.10+

### 安装步骤

#### 1. 克隆仓库

```bash
git clone https://github.com/Avalephh/KeenInsight.git
cd KeenInsight
```

#### 2. 后端设置

```bash
# 创建 Python 虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

#### 3. 前端设置

```bash
cd sysinsight_front

# 安装依赖
npm install

# 开发模式运行
npm run dev

# 生产构建
npm run build
```

#### 4. C++ 模块编译

```bash
# 在项目根目录
mkdir build
cd build
cmake ..
make
```

## 📖 使用指南

### 开发

```bash
# 启动后端服务
python main.py

# 启动前端开发服务器（新终端）
cd sysinsight_front
npm run dev
```

### 测试

```bash
# 前端单元测试
npm run test:unit

# 前端 E2E 测试
npm run test:e2e

# 代码检查
npm run lint
```

### 构建生产版本

```bash
# 前端构建
npm run build

# C++ 模块构建
make release
```

## 🔧 配置

详细的配置说明请参考各模块的文档：

- [前端配置](./sysinsight_front/README.md)
- [后端配置](./backend/README.md)
- [C++ 模块配置](./core/README.md)

## 📝 推荐开发环境

### IDE 推荐

**前端开发:**
- VS Code + [Vue (Official)](https://marketplace.visualstudio.com/items?itemName=Vue.volar)
- 禁用 Vetur 插件，使用官方 Vue 扩展

**后端开发:**
- PyCharm / VS Code + Python 扩展
- 启用类型检查和 Linting

**C++ 开发:**
- CLion / VS Code + C/C++ 扩展
- CMake 支持

### 浏览器工具

- [Vue.js DevTools](https://devtools.vuejs.org/) - Vue 调试工具
- 浏览器开发者工具的自定义对象格式化器

## 🤝 贡献指南

欢迎贡献代码！请遵循以下步骤：

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

## 📄 许可证

本项目采用 MIT 许可证。详见 [LICENSE](./LICENSE) 文件。

## 👨‍💻 作者

**Avalephh** - [GitHub Profile](https://github.com/Avalephh)

## 💬 支持与反馈

- 提交 [Issue](https://github.com/Avalephh/KeenInsight/issues) 报告问题
- 提交 [Pull Request](https://github.com/Avalephh/KeenInsight/pulls) 贡献代码
- 参与 [Discussions](https://github.com/Avalephh/KeenInsight/discussions) 讨论

## 🎯 路线图

- [ ] 完善系统监测模块
- [ ] 增强数据可视化功能
- [ ] 优化 C++ 核心模块性能
- [ ] 支持集群部署
- [ ] 添加告警通知功能

---

<div align="center">

⭐ 如果觉得项目有帮助，请给一个 Star！

</div>
