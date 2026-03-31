from rich.console import Console


class ExtendedConsole(Console):
    def error(self, message: str):
        self.print(f":boom: [bold red]ERROR[/bold red]: {message}")

    def warning(self, message: str):
        self.print(f":warning: [yellow]WARNING[/yellow]: {message}")

    def hint(self, message: str):
        self.print(f":bulb: [bright_cyan]HINT[/bright_cyan]: {message}")

    def success(self, message: str):
        self.print(f":white_check_mark: [green]SUCCESS[/green]: {message}")

    def info(self, message: str):
        self.print(f":memo: INFO: {message}")


console = ExtendedConsole(stderr=True)
