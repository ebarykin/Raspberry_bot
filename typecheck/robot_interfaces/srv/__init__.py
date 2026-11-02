"""Stand-in for robot_interfaces.srv. Runtime uses the generated ROS 2 module."""


class GetCapabilities:
    class Request:
        pass

    class Response:
        json: str

        def __init__(self) -> None:
            self.json = ""
