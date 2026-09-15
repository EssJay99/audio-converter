def register_blueprints(app):
    """Register all blueprints on the Flask app."""
    from .home import bp as home_route_bp
    from .convert import bp as convert_route_bp
    from .settings import bp as settings_route_bp
    from .tidal import bp as tidal_route_bp
    app.register_blueprint(home_route_bp)
    app.register_blueprint(convert_route_bp)
    app.register_blueprint(settings_route_bp)
    app.register_blueprint(tidal_route_bp)
