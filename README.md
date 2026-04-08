# KeenInsight

<div align="center">

**A Comprehensive System Intelligence & Performance Analytics Platform**

![Python](https://img.shields.io/badge/Python-59.7%25-blue)
![C++](https://img.shields.io/badge/C%2B%2B-20.6%25-brightgreen)
![Vue](https://img.shields.io/badge/Vue-8.8%25-4FC08D)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

</div>

## 📋 Project Overview

KeenInsight is a powerful system insight and performance analytics platform that integrates multiple technology stacks, providing deep system monitoring, performance optimization, and data analysis capabilities. The project adopts a front-end and back-end separation architecture, combined with high-performance C++ modules to achieve efficient data processing.

## 🌟 Core Features

- **System Monitoring** - Real-time monitoring of system runtime status and performance metrics
- **Performance Analysis** - In-depth analysis of system bottlenecks and optimization directions
- **Data Visualization** - Interactive dashboards based on Vue framework
- **High-Performance Processing** - C++ accelerated data processing modules
- **Scalable Architecture** - Modular design for easy extension and maintenance

## 🛠️ Technology Stack

| Technology | Percentage | Purpose |
|------------|-----------|---------|
| **Python** | 59.7% | Backend core logic, data processing |
| **C++** | 20.6% | High-performance computing, system-level operations |
| **Vue** | 8.8% | Frontend UI, interactive display |
| **TypeScript** | 2.5% | Type-safe frontend development |
| **Makefile** | 4.4% | Build and compilation management |
| **CMake** | 2.0% | C++ project building |

## 📦 Project Structure

```
KeenInsight/
├── sysinsight_front/          # Vue frontend application
├── backend/                   # Python backend service
├── core/                      # C++ high-performance modules
├── Makefile                   # Build scripts
├── CMakeLists.txt            # CMake build configuration
└── README.md                 # Project documentation
```

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- Node.js 16+ with npm
- C++ compiler (GCC/Clang)
- CMake 3.10+

### Installation Steps

#### 1. Clone the Repository

```bash
git clone https://github.com/Avalephh/KeenInsight.git
cd KeenInsight
```

#### 2. Backend Setup

```bash
# Create Python virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
```

#### 3. Frontend Setup

```bash
cd sysinsight_front

# Install dependencies
npm install

# Run in development mode
npm run dev

# Production build
npm run build
```

#### 4. Compile C++ Modules

```bash
# In project root directory
mkdir build
cd build
cmake ..
make
```

## 📖 Usage Guide

### Development

```bash
# Start backend service
python main.py

# Start frontend development server (in a new terminal)
cd sysinsight_front
npm run dev
```

### Testing

```bash
# Frontend unit tests
npm run test:unit

# Frontend E2E tests
npm run test:e2e

# Code linting
npm run lint
```

### Build for Production

```bash
# Frontend build
npm run build

# C++ module build
make release
```

## 🔧 Configuration

For detailed configuration instructions, please refer to the documentation of each module:

- [Frontend Configuration](./sysinsight_front/README.md)
- [Backend Configuration](./backend/README.md)
- [C++ Module Configuration](./core/README.md)

## 📝 Recommended Development Environment

### IDE Recommendations

**Frontend Development:**
- VS Code + [Vue (Official)](https://marketplace.visualstudio.com/items?itemName=Vue.volar)
- Disable Vetur extension, use official Vue extension

**Backend Development:**
- PyCharm / VS Code + Python extension
- Enable type checking and linting

**C++ Development:**
- CLion / VS Code + C/C++ extension
- CMake support

### Browser Tools

- [Vue.js DevTools](https://devtools.vuejs.org/) - Vue debugging tool
- Browser DevTools custom object formatters

## 🤝 Contributing

Contributions are welcome! Please follow these steps:

1. Fork this repository
2. Create a feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](./LICENSE) file for details.

## 👨‍💻 Author

**Avalephh** - [GitHub Profile](https://github.com/Avalephh)

## 💬 Support & Feedback

- Submit [Issues](https://github.com/Avalephh/KeenInsight/issues) to report problems
- Submit [Pull Requests](https://github.com/Avalephh/KeenInsight/pulls) to contribute code
