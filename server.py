"""Production entry point; --stdlib is explicitly for isolated local testing."""
import argparse
import logging
import os


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Private Travel Manager NAS server")
    parser.add_argument("--stdlib", action="store_true", help="isolated local development only; never expose this server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from app import create_app, create_application
    if args.stdlib:
        if args.host not in ("127.0.0.1", "::1", "localhost"):
            parser.error("The development server must bind to loopback. Use waitress in production.")
        from wsgiref.simple_server import make_server
        logging.warning("Isolated loopback development server; NOT for production or LAN access")
        with make_server(args.host, args.port, create_application()) as server:
            server.serve_forever()
    else:
        try:
            from waitress import serve
            application = create_app()
        except ImportError as exc:
            raise SystemExit("Production dependencies are missing. Build the Docker image; no automatic runtime installs are performed. For isolated loopback tests only, use --stdlib --host 127.0.0.1.") from exc
        serve(application, host=args.host, port=args.port, threads=2, connection_limit=32,
              channel_timeout=60, max_request_body_size=60 * 1024 * 1024,
              max_request_header_size=16384, inbuf_overflow=1024 * 1024,
              expose_tracebacks=False, clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
