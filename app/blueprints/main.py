from __future__ import annotations

from flask import Blueprint, render_template


main_bp = Blueprint("main", __name__)


@main_bp.get("/")
def home():
    """主页面：功能入口选择。"""
    return render_template("home.html")
