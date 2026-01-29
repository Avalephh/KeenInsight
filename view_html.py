#!/usr/bin/env python3
"""
简单的HTTP服务器，用于实时查看生成的HTML页面。
运行此脚本后，在浏览器中访问 http://localhost:8000 查看页面。
"""

import http.server
import socketserver
import os
import webbrowser
from pathlib import Path

PORT = 8000

class MyHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # 设置工作目录为 /root/dream/results，用于查看渲染后的HTML
        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
        super().__init__(*args, directory=base_dir, **kwargs)
    
    def translate_path(self, path):
        """重写路径翻译，确保能正确访问font目录下的文件"""
        # 移除查询参数
        path = path.split('?')[0]
        path = path.split('#')[0]
        
        # 如果路径以 /results/ 开头，直接映射到 results 目录
        if path.startswith('/results/'):
            base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
            file_path = os.path.join(base_dir, path.lstrip('/'))
            return file_path
        
        # 默认行为
        return super().translate_path(path)
    
    def end_headers(self):
        # 添加CORS头，允许跨域访问
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

def main():
    """启动HTTP服务器"""
    # 确保工作目录正确
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"))
    
    # 生成初始HTML（如果不存在或需要更新）
    from font_generate.generate_initial_html import generate_initial_diagnosis_html, main as gen_main
    
    results_dir = os.path.join(os.path.dirname(__file__), 'results')
    diagnosis_file = os.path.join(results_dir, 'diagnosis.html')
    
    if not os.path.exists(diagnosis_file):
        print("正在生成初始HTML文件...")
        gen_main()
    
    Handler = MyHTTPRequestHandler
    
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        url = f"http://localhost:{PORT}/diagnosis.html"
        print("=" * 60)
        print("HTML查看服务器已启动！")
        print("=" * 60)
        print(f"访问地址: {url}")
        print(f"诊断页面: http://localhost:{PORT}/diagnosis.html")
        print(f"调优建议: http://localhost:{PORT}/handling.html")
        print(f"多轮调优: http://localhost:{PORT}/multi-tune.html")
        print("=" * 60)
        print("提示: 点击'一键诊断'按钮进行诊断，或单独诊断每条SQL")
        print("按 Ctrl+C 停止服务器")
        print("=" * 60)
        
        # 检查文件是否存在
        if os.path.exists(diagnosis_file):
            print(f"✓ 找到文件: {diagnosis_file}")
        else:
            print(f"✗ 文件不存在: {diagnosis_file}")
        
        # 自动打开浏览器
        try:
            webbrowser.open(url)
        except:
            pass
        
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务器已停止")

if __name__ == '__main__':
    main()
