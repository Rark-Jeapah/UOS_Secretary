from setuptools import find_packages, setup


setup(
    name="sidae-secretary",
    version="0.1.0",
    description="Sidae Secretary local-first sync agent",
    license="MIT",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={"sidae_secretary": ["ops_dashboard_assets/*.html"]},
    python_requires=">=3.11",
    install_requires=[
        "typer>=0.12.3",
        "python-dotenv>=1.0.1",
        "requests>=2.31.0",
        "icalendar>=5.0.12",
        "python-dateutil>=2.9.0",
        "olefile>=0.47",
        "python-pptx>=1.0.2",
        "pypdf>=4.3.1",
        "playwright>=1.52.0",
    ],
    extras_require={"dev": ["pytest>=8.2.0"]},
    entry_points={"console_scripts": ["sidae=sidae_secretary.cli:app"]},
)
