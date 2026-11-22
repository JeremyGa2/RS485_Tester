"""RS485 COM Tester V6 — point d'entrée (utilisé aussi par PyInstaller).

  RS485_Tester.exe                      lance l'application
  RS485_Tester.exe --selftest           auto-tests du moteur (depuis les sources)
  RS485_Tester.exe --smoke out.json [dossier_captures]
                                        vérification complète sur la liaison simulée
"""
import sys


def _selftest() -> int:
    import os
    import unittest
    here = os.path.dirname(os.path.abspath(__file__))
    suite = unittest.defaultTestLoader.discover(os.path.join(here, "tests"), top_level_dir=here)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--smoke" in sys.argv:
        i = sys.argv.index("--smoke")
        out = sys.argv[i + 1]
        shots = sys.argv[i + 2] if len(sys.argv) > i + 2 else None
        from rs485tester.gui import smoke
        smoke(out, shots)
        sys.exit(0)
    from rs485tester.gui import main
    main()
