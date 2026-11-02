from setuptools import find_packages, setup

package_name = "robot_gateway"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (
            "share/" + package_name + "/launch",
            ["launch/gateway_rosbridge.launch.py"],
        ),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="teleop-robot",
    maintainer_email="dev@teleop-robot.local",
    description="Operator gateway: stock rosbridge and GetCapabilities.",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "get_capabilities = robot_gateway.get_capabilities:main",
        ],
    },
)
