# Processo_stacking - script da eseguire DENTRO Siril (menu Scripts)
# Richiede Siril 1.4+ con il modulo Python sirilpy.
import os
import sys
import glob
import shutil

import sirilpy as s
s.ensure_installed("PyQt6")

from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QGroupBox, QLabel, QLineEdit, QPushButton,
                             QCheckBox, QFileDialog, QMessageBox,
                             QComboBox, QDoubleSpinBox)
from PyQt6.QtCore import QThread, pyqtSignal

LIGHTS_WORK_DIR = "converted_and_calibrated_and_registered_lights"
RAW_EXT = ('.fit', '.fits', '.cr2', '.cr3', '.arw', '.nef', '.dng', '.tif', '.tiff')


def run_button_style(color):
    return f"background-color: {color}; color: white; font-weight: bold; padding: 15px; border-radius: 5px; font-size: 16px;"


# ==========================================
# THREAD LAVORATORE: INVIA I COMANDI A SIRIL
# ==========================================
class SirilWorker(QThread):
    finished_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)

    def __init__(self, siril, project_folder, opts, sigmas):
        super().__init__()
        self.siril = siril
        self.folder = project_folder
        self.o = opts
        self.sig = sigmas
        self.steps = []   # lista di (descrizione, [comandi siril], funzione di pulizia o None)

    # ---------- utilità ----------
    def path(self, *parts):
        return os.path.join(self.folder, *parts)

    def rm_tree(self, name):
        shutil.rmtree(self.path(name), ignore_errors=True)

    def rm_match(self, folder, prefix):
        for f in glob.glob(self.path(folder, f"{prefix}*.fit*")):
            os.remove(f)

    def add_step(self, label, commands, cleanup_folder=None, cleanup_match=None):
        cleanup = None
        if self.o['delete']:
            if cleanup_folder and cleanup_match:
                cleanup = lambda: self.rm_match(cleanup_folder, cleanup_match)
            elif cleanup_folder:
                cleanup = lambda: self.rm_tree(cleanup_folder)
        self.steps.append((label, commands, cleanup))

    def cd(self, rel):
        # Percorso assoluto tra virgolette: funziona anche con spazi
        return f'cd "{self.path(rel)}"'

    # ---------- costruzione pipeline ----------
    def prepare_lights(self):
        if not self.o['mini_test']:
            return "lights"
        src, dst = self.path("lights"), self.path("lights_mini_test")
        if not os.path.isdir(src):
            raise Exception(f"Cartella 'lights' non trovata in {self.folder}")
        self.rm_tree("lights_mini_test")
        os.makedirs(dst)
        files = sorted(f for f in os.listdir(src) if f.lower().endswith(RAW_EXT))
        for f in files[:30]:
            shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
        return "lights_mini_test"

    def build_steps(self, lights_folder):
        o, sig = self.o, self.sig
        drizzle = "Drizzle 1x" in o['light_process']

        if not o['only_reg_stack']:
            darkflat = o['calib_type'] == "Darkflat"
            frame = "darkflat" if darkflat else "bias"
            folder = "darkflats" if darkflat else "biases"
            conv = f"converted_{folder}"
            master = f"{frame}_stacked"

            self.add_step(f"Conversione e stacking {frame}", [
                self.cd(folder), f"convert {frame} -out=../{conv}",
                self.cd(conv), f'stack {frame} rej 3 3 -nonorm -out=../masters/{master}',
            ], cleanup_folder=conv)

            self.add_step("Conversione e stacking dark", [
                self.cd("darks"), "convert dark -out=../converted_darks",
                self.cd("converted_darks"), f'stack dark rej 3 3 -nonorm -out=../masters/dark_stacked',
            ], cleanup_folder="converted_darks")

            flag = f'-dark=../masters/{master}' if darkflat else f'-bias=../masters/{master}'
            self.add_step("Conversione e calibrazione flat", [
                self.cd("flats"), "convert flat -out=../converted_and_calibrated_flats",
                self.cd("converted_and_calibrated_flats"), f"calibrate flat {flag}",
            ], cleanup_folder="converted_and_calibrated_flats", cleanup_match="flat")

            self.add_step("Stacking flat", [
                self.cd("converted_and_calibrated_flats"),
                f'stack pp_flat rej 3 3 -norm=mul -out=../masters/pp_flat_stacked',
            ], cleanup_folder="converted_and_calibrated_flats")

            calib = (f'calibrate light -dark=../masters/dark_stacked -flat=../masters/pp_flat_stacked '
                     f"-cc=dark {sig['cal_low']} {sig['cal_high']} -cfa -equalize_cfa")
            if not drizzle:
                calib += " -debayer"
            self.add_step("Calibrazione light", [
                self.cd(lights_folder), f"convert light -out=../{LIGHTS_WORK_DIR}",
                self.cd(LIGHTS_WORK_DIR), calib,
            ], cleanup_folder=LIGHTS_WORK_DIR, cleanup_match="light")
        else:
            self.add_step("Importazione sequenza pre-calibrata", [
                self.cd(lights_folder), f"convert pp_light -out=../{LIGHTS_WORK_DIR}",
            ])

        prefix = "pp_light"

        if o['extract_bkg']:
            self.add_step("Estrazione gradiente di sfondo", [
                self.cd(LIGHTS_WORK_DIR),
                f"seqsubsky {prefix} 1 -nodither -samples=20 -tolerance=1.0",
            ], cleanup_folder=LIGHTS_WORK_DIR, cleanup_match=prefix)
            prefix = f"bkg_{prefix}"

        if o['stop_after_calib']:
            return None

        if drizzle:
            reg = f"register {prefix} -drizzle -scale=1 -pixfrac=1 -kernel=square"
            if not o['only_reg_stack']:
                reg += " -flat=../masters/pp_flat_stacked"
        else:
            reg = f"register {prefix}"
        self.add_step("Registrazione", [self.cd(LIGHTS_WORK_DIR), reg],
                      cleanup_folder=LIGHTS_WORK_DIR, cleanup_match=prefix)
        prefix = f"r_{prefix}"

        out_name = "risultato_mini_test" if o['mini_test'] else "risultato"
        rej = " -rejmaps" if o['rejmaps'] else ""
        self.add_step("Stacking finale", [
            self.cd(LIGHTS_WORK_DIR),
            f"stack {prefix} rej l {sig['stk_low']} {sig['stk_high']}{rej} -norm=addscale -output_norm -rgb_equal -32b -out={out_name}",
            f"load {out_name}", "mirrorx -bottomup", f"save ../{out_name}", "close",
        ])
        return out_name

    # ---------- esecuzione ----------
    def run(self):
        try:
            # Rimuove residui di run precedenti per non mescolare i frame
            self.rm_tree(LIGHTS_WORK_DIR)
            lights_folder = self.prepare_lights()
            out_name = self.build_steps(lights_folder)

            total = len(self.steps)
            for i, (label, commands, cleanup) in enumerate(self.steps):
                self.status_signal.emit(f"{i + 1}/{total} - {label}")
                self.siril.log(f">>> {label}...", s.LogColor.SALMON)
                self.siril.update_progress(label, i / total)
                for c in commands:
                    try:
                        self.siril.cmd(c)
                    except s.CommandError as e:
                        raise Exception(f"Passo '{label}' fallito sul comando:\n{c}\n\n{e}") from e
                if cleanup:
                    cleanup()

            self.finalize(out_name)
            self.siril.reset_progress()
            if out_name is None:
                msg = "Preprocessing parziale completato! I file sono nella cartella di lavoro."
            else:
                # Mostra il file realmente salvato (l'estensione dipende dalle preferenze di Siril)
                saved = sorted(glob.glob(self.path(f"{out_name}.fit*")))
                where = saved[0] if saved else self.path(out_name)
                msg = f"Stacking completato!\nImmagine finale salvata in:\n{where}"
            self.siril.log(msg, s.LogColor.GREEN)
            self.finished_signal.emit(msg)
        except Exception as e:
            try:
                self.siril.reset_progress()
                self.siril.log(f"!!! Processo interrotto: {e}", s.LogColor.RED)
            except Exception:
                pass
            self.error_signal.emit(str(e))
        finally:
            # Siril resta nella cartella del progetto
            try:
                self.siril.cmd(f'cd "{self.folder}"')
            except Exception:
                pass

    def finalize(self, out_name):
        if out_name is not None:
            if self.o['rejmaps']:
                for f in glob.glob(self.path(LIGHTS_WORK_DIR, "*_rejmap*.fit*")):
                    shutil.move(f, self.path(os.path.basename(f)))
            if self.o['delete']:
                self.rm_tree(LIGHTS_WORK_DIR)
        if self.o['mini_test']:
            self.rm_tree("lights_mini_test")


# ==========================================
# INTERFACCIA GRAFICA
# ==========================================
class SirilApp(QWidget):
    def __init__(self, siril):
        super().__init__()
        self.siril = siril
        self.worker = None
        self.initUI()

    def initUI(self):
        self.setWindowTitle("Processo_stacking")
        self.resize(750, 600)
        main_layout = QVBoxLayout()

        # --- Cartella progetto (precompilata con la cartella di lavoro di Siril) ---
        paths_group = QGroupBox("Cartella di Progetto")
        folder_layout = QHBoxLayout()
        folder_layout.addWidget(QLabel("Cartella Progetto:"))
        try:
            wd = self.siril.get_siril_wd()
        except Exception:
            wd = ""
        self.folder_input = QLineEdit(wd)
        folder_layout.addWidget(self.folder_input)
        self.btn_browse = QPushButton("Sfoglia...")
        self.btn_browse.clicked.connect(self.browse_folder)
        folder_layout.addWidget(self.btn_browse)
        paths_group.setLayout(folder_layout)
        main_layout.addWidget(paths_group)

        # --- Opzioni ---
        options_group = QGroupBox("Parametri di Calibrazione e Allineamento")
        options_layout = QVBoxLayout()

        self.chk_mini_test = QCheckBox("Test Rapido: Elabora solo max 30 Light frame (ottimo per testare le Rejmap)")
        self.chk_mini_test.setStyleSheet("color: #ff9900; font-weight: bold;")
        options_layout.addWidget(self.chk_mini_test)

        calib_layout = QHBoxLayout()
        calib_layout.addWidget(QLabel("Tipo calibrazione:"))
        self.combo_calib = QComboBox()
        self.combo_calib.addItems(["Darkflat", "Bias"])
        calib_layout.addWidget(self.combo_calib)
        calib_layout.addStretch()
        calib_layout.addWidget(QLabel("Metodo Lights:"))
        self.combo_light = QComboBox()
        self.combo_light.addItems(["Debayer Standard (Senza Drizzle)", "Drizzle 1x (Senza Debayer)"])
        calib_layout.addWidget(self.combo_light)
        options_layout.addLayout(calib_layout)

        self.chk_bkg = QCheckBox("Esegui Estrazione Gradiente Sfondo sui singoli light frame")
        options_layout.addWidget(self.chk_bkg)
        self.chk_stop_calib = QCheckBox("Fermati dopo la Calibrazione (Salta Registrazione e Stacking)")
        options_layout.addWidget(self.chk_stop_calib)
        self.chk_only_reg_stack = QCheckBox("Esegui SOLO Registrazione e Stacking (Usa sequenza 'pp_light' nella cartella 'lights')")
        options_layout.addWidget(self.chk_only_reg_stack)
        self.chk_delete = QCheckBox("Elimina file temporanei durante il processo (consigliato)")
        self.chk_delete.setChecked(True)
        options_layout.addWidget(self.chk_delete)

        options_group.setLayout(options_layout)
        main_layout.addWidget(options_group)

        # --- Sigma e rejmaps ---
        sigma_group = QGroupBox("Parametri Sigma e Test Rejmaps")
        sigma_layout = QVBoxLayout()

        def spin(value):
            sb = QDoubleSpinBox()
            sb.setValue(value)
            sb.setSingleStep(0.1)
            return sb

        row = QHBoxLayout()
        row.addWidget(QLabel("Sigma Calibrazione (Low):"))
        self.spin_cal_low = spin(3.0); row.addWidget(self.spin_cal_low)
        row.addWidget(QLabel("(High):"))
        self.spin_cal_high = spin(3.0); row.addWidget(self.spin_cal_high)
        sigma_layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Sigma Stacking (Low):"))
        self.spin_stk_low = spin(3.0); row.addWidget(self.spin_stk_low)
        row.addWidget(QLabel("(High):"))
        self.spin_stk_high = spin(3.0); row.addWidget(self.spin_stk_high)
        sigma_layout.addLayout(row)

        self.chk_rejmaps = QCheckBox("Genera e salva Rejection Maps (Rejmaps) nello stacking finale")
        self.chk_rejmaps.setChecked(True)
        sigma_layout.addWidget(self.chk_rejmaps)
        sigma_group.setLayout(sigma_layout)
        main_layout.addWidget(sigma_group)

        # --- Avvio e stato ---
        self.btn_run = QPushButton("Avvia Processo in Siril")
        self.btn_run.setStyleSheet(run_button_style("#28a745"))
        self.btn_run.clicked.connect(self.start_processing)
        main_layout.addWidget(self.btn_run)

        self.status_label = QLabel("Pronto.")
        main_layout.addWidget(self.status_label)

        self.setLayout(main_layout)

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleziona la cartella del progetto", self.folder_input.text())
        if folder:
            self.folder_input.setText(folder)

    def start_processing(self):
        project_folder = self.folder_input.text().strip()

        if self.chk_stop_calib.isChecked() and self.chk_only_reg_stack.isChecked():
            QMessageBox.warning(self, "Conflitto Opzioni", "Hai selezionato sia di fermarti dopo la calibrazione, sia di saltarla. Scegli una sola delle due opzioni.")
            return
        if not project_folder or not os.path.isdir(project_folder):
            QMessageBox.critical(self, "Errore", "Seleziona una cartella di progetto valida prima di iniziare.")
            return

        opts = {
            'delete': self.chk_delete.isChecked(),
            'calib_type': self.combo_calib.currentText(),
            'light_process': self.combo_light.currentText(),
            'rejmaps': self.chk_rejmaps.isChecked(),
            'extract_bkg': self.chk_bkg.isChecked(),
            'mini_test': self.chk_mini_test.isChecked(),
            'stop_after_calib': self.chk_stop_calib.isChecked(),
            'only_reg_stack': self.chk_only_reg_stack.isChecked(),
        }
        sigmas = {
            'cal_low': self.spin_cal_low.value(), 'cal_high': self.spin_cal_high.value(),
            'stk_low': self.spin_stk_low.value(), 'stk_high': self.spin_stk_high.value(),
        }

        # Il pulsante resta disattivato fino alla fine reale dell'elaborazione
        self.btn_run.setEnabled(False)
        self.btn_run.setStyleSheet(run_button_style("gray"))

        self.worker = SirilWorker(self.siril, project_folder, opts, sigmas)
        self.worker.status_signal.connect(self.status_label.setText)
        self.worker.error_signal.connect(self.process_error)
        self.worker.finished_signal.connect(self.process_finished)
        self.worker.start()

    def process_error(self, err_msg):
        self.status_label.setText("Errore.")
        QMessageBox.critical(self, "Errore", err_msg)
        self.reset_button()

    def process_finished(self, msg):
        self.status_label.setText(msg)
        QMessageBox.information(self, "Completato", msg)
        self.reset_button()

    def reset_button(self):
        self.btn_run.setEnabled(True)
        self.btn_run.setStyleSheet(run_button_style("#28a745"))

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "Elaborazione in corso", "Attendi la fine dell'elaborazione prima di chiudere la finestra.")
            event.ignore()
            return
        event.accept()


# ==========================================
# AVVIO
# ==========================================
def main():
    siril = s.SirilInterface()
    try:
        siril.connect()
    except s.SirilConnectionError:
        print("Impossibile connettersi a Siril: avvia questo script dal menu Scripts di Siril.")
        sys.exit(1)

    app = QApplication.instance() or QApplication(sys.argv)
    window = SirilApp(siril)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
